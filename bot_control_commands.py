"""General information and developer controls for the Discord bot."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import discord
from discord.ext import commands

from admin_commands import AdminConfirmationView


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, total = divmod(total, 86_400)
    hours, total = divmod(total, 3_600)
    minutes, seconds = divmod(total, 60)
    parts = []
    if days:
        parts.append(f"{days} يوم")
    if hours or days:
        parts.append(f"{hours} ساعة")
    if minutes or hours or days:
        parts.append(f"{minutes} دقيقة")
    parts.append(f"{seconds} ثانية")
    return "، ".join(parts)


class StopBotView(discord.ui.View):
    """Developer-only confirmation for restarting or stopping the service."""

    def __init__(self, bot, developer_id, *, shutdown, shutdown_marker):
        super().__init__(timeout=45)
        self.bot = bot
        self.developer_id = developer_id
        self.shutdown = shutdown
        self.shutdown_marker = Path(shutdown_marker)
        self.finished = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "التأكيد متاح للمطور فقط.", ephemeral=True
            )
            return False
        return True

    def _finish(self):
        self.finished = True
        for item in self.children:
            item.disabled = True
        self.stop()

    @discord.ui.button(label="تأكيد", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self.finished:
            await interaction.response.send_message("انتهت صلاحية هذا التأكيد.", ephemeral=True)
            return
        if self.shutdown:
            try:
                self.shutdown_marker.parent.mkdir(parents=True, exist_ok=True)
                self.shutdown_marker.write_text("requested\n", encoding="utf-8")
            except OSError:
                self._finish()
                await interaction.response.edit_message(
                    content="تعذر تجهيز إيقاف سير العمل؛ لم يتم إيقاف البوت.",
                    view=self,
                )
                return
        self._finish()
        if self.shutdown:
            text = "سيتم إيقاف البوت وسير العمل. لإعادته شغّله من Replit."
        else:
            text = "سيعاد تشغيل البوت تلقائياً خلال بضع ثوانٍ."
        await interaction.response.edit_message(content=text, embed=None, view=self)
        await asyncio.sleep(0.5)
        await self.bot.close()

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.finished:
            await interaction.response.send_message("انتهت صلاحية هذا التأكيد.", ephemeral=True)
            return
        self._finish()
        await interaction.response.edit_message(
            content="تم الإلغاء؛ لم يتغير وضع البوت.", embed=None, view=self
        )

    async def on_timeout(self):
        if self.finished:
            return
        self._finish()


def setup_bot_control_commands(
    bot, *, developer_id, conversations, game_store, admin_engine,
    process_started_at, shutdown_marker,
):
    """Register all safe information and developer controls once."""
    if getattr(bot, "_bot_control_commands_registered", False):
        return

    async def is_developer(ctx):
        return ctx.author.id == developer_id

    def developer_only(function):
        return commands.check(is_developer)(function)

    @bot.command(name="about")
    async def about(ctx):
        embed = discord.Embed(
            title="عن البوت",
            description="بوت ديسكورد للدردشة بالذكاء الاصطناعي وألعاب الرصيد الافتراضي.",
            color=0x4A90E2,
        )
        embed.add_field(name="المطور", value="mohammedbg")
        embed.add_field(name="الرصيد", value="افتراضي للألعاب فقط")
        await ctx.reply(embed=embed, mention_author=False)

    @bot.command(name="ping")
    async def ping(ctx):
        latency_ms = round(bot.latency * 1000)
        await ctx.reply(f"🏓 زمن استجابة Discord: `{latency_ms} ms`")

    @bot.command(name="uptime")
    async def uptime(ctx):
        elapsed = time.monotonic() - process_started_at
        await ctx.reply(f"⏱️ مدة تشغيل هذه النسخة: {_format_duration(elapsed)}")

    @bot.command(name="memory")
    async def memory(ctx):
        count = len(conversations.get(ctx.author.id, []))
        await ctx.reply(
            f"🧠 لديك `{count}` رسالة محفوظة مؤقتاً في سياق المحادثة "
            "(حد أقصى 10). لا أعرض محتوى المحادثة هنا."
        )

    @bot.command(
        name="status",
        aliases=["selfupdate", "تحديث_ذاتي", "status_bot"],
    )
    @developer_only
    async def status(ctx):
        snapshot = game_store.snapshot()
        embed = discord.Embed(title="حالة البوت", color=0x2ECC71)
        embed.add_field(name="الحالة", value="متصل")
        embed.add_field(name="زمن الاستجابة", value=f"{round(bot.latency * 1000)} ms")
        embed.add_field(
            name="مدة التشغيل",
            value=_format_duration(time.monotonic() - process_started_at),
        )
        embed.add_field(name="السيرفرات", value=str(len(bot.guilds)))
        embed.add_field(name="اللاعبون المحفوظون", value=str(len(snapshot.get("users", {}))))
        embed.add_field(name="سياقات AI المؤقتة", value=str(len(conversations)))
        await ctx.reply(embed=embed, mention_author=False)

    @bot.command(name="say")
    @developer_only
    async def say(ctx, *, message: str = None):
        if not message or not message.strip():
            await ctx.reply("الاستخدام: `!say <رسالتك>`")
            return
        if len(message) > 1900:
            await ctx.reply("الرسالة طويلة؛ الحد الأقصى 1900 حرف.")
            return
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    @bot.command(name="welcometest")
    @developer_only
    async def welcome_test(ctx):
        if ctx.guild is None:
            await ctx.reply("هذا الاختبار متاح داخل سيرفر.")
            return
        display_name = discord.utils.escape_markdown(ctx.author.display_name)[:100]
        embed = discord.Embed(
            title="معاينة ترحيب تجريبية",
            description=f"مرحباً **{display_name}** في **{discord.utils.escape_markdown(ctx.guild.name)[:100]}**!",
            color=0x2ECC71,
        )
        embed.set_footer(text="معاينة فقط؛ لا يوجد نظام ترحيب تلقائي مهيأ.")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bot.command(name="resetcool")
    @developer_only
    async def reset_cooldowns(ctx):
        count = len(game_store.snapshot().get("cooldowns", {}))
        view = AdminConfirmationView(
            developer_id,
            lambda: (
                f"تم تصفير {admin_engine.reset_cooldowns()} فترة انتظار. "
                "أُنشئت نسخة احتياطية قبل التعديل."
            ),
        )
        embed = discord.Embed(
            title="تصفير فترات انتظار اللعبة؟",
            description=(
                f"سيتم تصفير {count} فترة انتظار لكل اللاعبين. "
                "ستُحفظ نسخة احتياطية محلية قبل التعديل."
            ),
            color=0xE67E22,
        )
        view.message = await ctx.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="restart")
    @developer_only
    async def restart(ctx):
        view = StopBotView(
            bot, developer_id, shutdown=False, shutdown_marker=shutdown_marker
        )
        view.message = await ctx.send(
            "سيُعاد تشغيل البوت بعد التأكيد. هل تريد المتابعة؟",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="shutdown")
    @developer_only
    async def shutdown(ctx):
        view = StopBotView(
            bot, developer_id, shutdown=True, shutdown_marker=shutdown_marker
        )
        view.message = await ctx.send(
            "سيُوقف هذا الأمر البوت وسير العمل بالكامل؛ ستحتاج إلى تشغيله يدوياً من Replit. هل تريد المتابعة؟",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    help_text = {
        "about": "!about — معلومات عن البوت.",
        "ping": "!ping — قياس زمن استجابة Discord.",
        "uptime": "!uptime — مدة تشغيل نسخة البوت الحالية.",
        "memory": "!memory — عدد رسائل سياق محادثتك المؤقتة دون عرض محتواها.",
        "status": "!status — حالة البوت والإحصائيات التشغيلية (للمطور فقط).",
        "say": "!say <رسالة> — إرسال رسالة في القناة دون تفعيل المنشنات (للمطور فقط).",
        "welcometest": "!welcometest — عرض معاينة ترحيب تجريبية (للمطور فقط).",
        "resetcool": "!resetcool — تصفير فترات الانتظار بعد التأكيد ونسخة احتياطية (للمطور فقط).",
        "restart": "!restart — إعادة تشغيل البوت بعد التأكيد (للمطور فقط).",
        "shutdown": "!shutdown — إيقاف البوت وسير العمل بعد التأكيد (للمطور فقط).",
    }
    for name, help_text_value in help_text.items():
        command = bot.get_command(name)
        command.help = help_text_value
        if name in {"status", "say", "welcometest", "resetcool", "restart", "shutdown"}:
            command.extras["developer_control"] = True

    bot._bot_control_commands_registered = True