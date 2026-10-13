"""Discord adapters for the persistent, virtual-credit casino."""

import logging
import re

import discord

from casino_engine import (
    CasinoError, balance, buy_ticket, draw_lottery, lottery_status, play,
    positive_amount, reward, settle_challenge, transfer,
)

logger = logging.getLogger(__name__)


async def _member(ctx, token):
    """Resolve a guild member even when the Members privileged intent is off."""
    match = re.fullmatch(r"<@!?(\d+)>|(\d+)", token or "")
    if not match or ctx.guild is None:
        raise CasinoError("❌ اذكر عضواً في السيرفر: @user أو معرّفه.")
    uid = int(match.group(1) or match.group(2))
    if uid == ctx.author.id:
        raise CasinoError("❌ لا يمكنك اختيار نفسك.")
    # Message mentions include member objects without a populated guild cache.
    member = next(
        (mentioned for mentioned in getattr(getattr(ctx, "message", None), "mentions", ())
         if mentioned.id == uid and isinstance(mentioned, discord.Member)
         and mentioned.guild.id == ctx.guild.id), None)
    if member is None:
        member = ctx.guild.get_member(uid)
    if member is None:
        try:
            member = await ctx.guild.fetch_member(uid)
        except discord.NotFound:
            raise CasinoError("❌ العضو غير موجود في هذا السيرفر.") from None
        except (discord.Forbidden, discord.HTTPException):
            raise CasinoError("❌ تعذر التحقق من عضوية المستخدم الآن. حاول لاحقاً.") from None
    if member.bot:
        raise CasinoError("❌ العضو غير موجود في السيرفر أو هو بوت.")
    return member


async def _error(ctx, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except CasinoError as exc:
        await ctx.reply(str(exc))
        return None


class ChallengeView(discord.ui.View):
    def __init__(self, store, challenger, opponent, amount):
        super().__init__(timeout=60)
        self.store = store
        self.challenger = challenger
        self.opponent = opponent
        self.amount = amount
        self.finished = False
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ القبول أو الرفض متاح فقط للشخص المتحدى.", ephemeral=True)
            return False
        return True

    def _disable(self):
        self.finished = True
        for button in self.children:
            button.disabled = True
        self.stop()

    @discord.ui.button(label="أقبل التحدي", style=discord.ButtonStyle.success)
    async def accept(self, interaction, button):
        if self.finished:
            await interaction.response.send_message("انتهى هذا التحدي.", ephemeral=True)
            return
        # Set before yielding control; competing callbacks cannot settle twice.
        self._disable()
        try:
            winner = settle_challenge(self.store, self.challenger.id,
                                      self.opponent.id, self.amount)
            text = (f"⚔️ {self.challenger.mention} ضد {self.opponent.mention}\n"
                    f"الرهان: {self.amount:,} رصيد افتراضي. "
                    f"الفائز: {self.challenger.mention if winner == self.challenger.id else self.opponent.mention}")
        except CasinoError as exc:
            text = f"❌ لم يبدأ التحدي؛ لم يُخصم أي رصيد. {exc}"
        await interaction.response.edit_message(content=text, view=self)

    @discord.ui.button(label="أرفض التحدي", style=discord.ButtonStyle.danger)
    async def decline(self, interaction, button):
        if self.finished:
            await interaction.response.send_message("انتهى هذا التحدي.", ephemeral=True)
            return
        self._disable()
        await interaction.response.edit_message(content="تم رفض التحدي. لم يُخصم أي رصيد.", view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self._disable()
        if self.message:
            try:
                await self.message.edit(content="انتهت مهلة التحدي. لم يُخصم أي رصيد.", view=self)
            except discord.HTTPException:
                logger.warning("Could not update expired casino challenge.")


def setup_casino_commands(bot, store, developer_id):
    """Register casino commands once, synchronously, before connecting the bot."""
    if getattr(bot, "_casino_commands_registered", False):
        return
    bot._casino_commands_registered = True

    async def gamble(ctx, game, stake, usage, **options):
        if stake is None or any(value is None for value in options.values()):
            await ctx.reply(f"⚠️ الاستخدام: `{usage}`")
            return
        outcome = await _error(ctx, play, store, ctx.author.id, game, stake, **options)
        if outcome is None:
            return
        delta, detail, current = outcome
        status = f"ربحت +{delta:,}" if delta > 0 else f"خسرت {delta:,}" if delta < 0 else "تعادل"
        await ctx.reply(f"{detail}\n{'🎉' if delta > 0 else '❌' if delta < 0 else '⚖️'} "
                        f"{status} رصيد افتراضي\n💰 رصيدك: {current:,}")

    @bot.command(name="ماين", aliases=["mine", "نرد"])
    async def mine(ctx, amount: str = None, number: str = None):
        await gamble(ctx, "mine", amount, "!ماين <مبلغ> [رقم 1-6]",
                     **({"number": number} if number is not None else {}))

    @bot.command(name="كنز", aliases=["treasure"])
    async def treasure(ctx, amount: str = None):
        await gamble(ctx, "treasure", amount, "!كنز <مبلغ>")

    @bot.command(name="ابراج", aliases=["towers", "برج"])
    async def towers(ctx, amount: str = None):
        await gamble(ctx, "towers", amount, "!ابراج <مبلغ>")

    @bot.command(name="صاروخ", aliases=["rocket", "crash"])
    async def rocket(ctx, amount: str = None):
        await gamble(ctx, "rocket", amount, "!صاروخ <مبلغ>")

    @bot.command(name="استثمار", aliases=["invest"])
    async def invest(ctx, amount: str = None):
        await gamble(ctx, "invest", amount, "!استثمار <مبلغ>")

    @bot.command(name="تداول", aliases=["trade", "تريد"])
    async def trade(ctx, amount: str = None):
        await gamble(ctx, "trade", amount, "!تداول <مبلغ>")

    @bot.command(name="سلوتس", aliases=["slots", "slot"])
    async def slots(ctx, amount: str = None):
        await gamble(ctx, "slots", amount, "!سلوتس <مبلغ>")

    @bot.command(name="عجلة", aliases=["wheel", "حظ"])
    async def wheel(ctx, amount: str = None):
        await gamble(ctx, "wheel", amount, "!عجلة <مبلغ>")

    @bot.command(name="روليت", aliases=["roulette"])
    async def roulette(ctx, amount: str = None, choice: str = None, number: str = None):
        await gamble(ctx, "roulette", amount,
                     "!روليت <مبلغ> أحمر/أسود/أخضر أو !روليت <مبلغ> رقم <0-36>",
                     choice=choice, **({"number": number} if choice in ("رقم", "number") else {}))

    @bot.command(name="بلاكجاك", aliases=["blackjack", "bj"])
    async def blackjack(ctx, amount: str = None):
        await gamble(ctx, "blackjack", amount, "!بلاكجاك <مبلغ>")

    @bot.command(name="سباق", aliases=["race", "خيل"])
    async def race(ctx, amount: str = None, horse: str = None):
        await gamble(ctx, "race", amount, "!سباق <مبلغ> <حصان 1-5>", horse=horse)

    @bot.command(name="رماية", aliases=["shoot", "aim"])
    async def shoot(ctx, amount: str = None):
        await gamble(ctx, "shoot", amount, "!رماية <مبلغ>")

    @bot.command(name="راتب", aliases=["salary", "daily"])
    async def salary(ctx):
        result = await _error(ctx, reward, store, ctx.author.id, "salary")
        if result is not None:
            amount, current = result
            await ctx.reply(f"💼 حصلت على +{amount:,} رصيد افتراضي. رصيدك: {current:,}. يعود بعد 24 ساعة.")

    @bot.command(name="بخشيش", aliases=["tip", "bakshish"])
    async def tip(ctx):
        result = await _error(ctx, reward, store, ctx.author.id, "bakshish")
        if result is not None:
            amount, current = result
            await ctx.reply(f"🎁 بخشيش: +{amount:,} رصيد افتراضي. رصيدك: {current:,}. يعود بعد 5 دقائق.")

    @bot.command(name="فلوسي", aliases=["balance"])
    async def show_balance(ctx):
        current = balance(store, ctx.author.id)
        await ctx.reply(f"💰 رصيدك الافتراضي: {current:,}")

    @bot.command(name="تحويل", aliases=["transfer"])
    async def send_credits(ctx, target: str = None, amount: str = None):
        if target is None or amount is None:
            await ctx.reply("⚠️ الاستخدام: `!تحويل @user <مبلغ>`")
            return
        try:
            member = await _member(ctx, target)
        except CasinoError as exc:
            await ctx.reply(str(exc))
            return
        result = await _error(ctx, transfer, store, ctx.author.id, member.id, amount)
        if result is not None:
            await ctx.reply(f"✅ حوّلت {int(amount):,} رصيد افتراضي إلى {member.mention}. رصيدك: {result:,}")

    @bot.command(name="يانصيب", aliases=["lottery", "لوتو"])
    async def lottery(ctx, amount: str = None):
        if amount is None:
            pot, tickets = lottery_status(store)
            await ctx.reply(f"🎟️ اليانصيب: الجائزة {pot:,} رصيد افتراضي؛ مجموع التذاكر {tickets:,}.\n"
                            "`!يانصيب <مبلغ>` لشراء تذاكر بقيمة المبلغ. فرصة الفوز نسبية لقيمة التذاكر. "
                            "السحب يدوي من المطور بـ `!يانصيب سحب`.")
            return
        if amount in ("سحب", "draw"):
            await perform_draw(ctx)
            return
        result = await _error(ctx, buy_ticket, store, ctx.author.id, amount)
        if result is not None:
            await ctx.reply(f"🎟️ اشتريت تذاكر بقيمة {int(amount):,} رصيد افتراضي. الجائزة: {result:,}.")

    async def perform_draw(ctx):
        if ctx.author.id != developer_id:
            await ctx.reply("❌ السحب متاح للمطور فقط.")
            return
        result = await _error(ctx, draw_lottery, store)
        if result is not None:
            winner, pot = result
            await ctx.reply(f"🎉 فاز <@{winner}> بجائزة {pot:,} رصيد افتراضي!",
                            allowed_mentions=discord.AllowedMentions.none())

    @bot.command(name="يانصيب_سحب", aliases=["lottery_draw"])
    async def lottery_draw(ctx):
        await perform_draw(ctx)

    @bot.command(name="تحدي", aliases=["challenge"])
    async def challenge(ctx, target: str = None, amount: str = None):
        if target is None or amount is None:
            await ctx.reply("⚠️ الاستخدام: `!تحدي @user <مبلغ>`")
            return
        try:
            member = await _member(ctx, target)
        except CasinoError as exc:
            await ctx.reply(str(exc))
            return
        valid = await _error(ctx, positive_amount, amount)
        if valid is None:
            return
        if member.id == ctx.author.id:
            await ctx.reply("❌ لا يمكنك تحدي نفسك.")
            return
        view = ChallengeView(store, ctx.author, member, valid)
        view.message = await ctx.reply(
            f"⚔️ {member.mention}، هل تقبل تحدي {ctx.author.mention} برهان {valid:,} رصيد افتراضي؟ "
            "لن يُخصم الرهان من أي طرف إلا عند القبول. المهلة 60 ثانية.",
            view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

    @bot.command(name="كازينو", aliases=["casino"])
    async def casino_menu(ctx):
        current = balance(store, ctx.author.id)
        embed = discord.Embed(
            title="🎰 كازينو الرصيد الافتراضي",
            description=f"رصيدك: **{current:,}**. رصيد اللعبة افتراضي فقط؛ لا شراء بأموال حقيقية ولا سحب نقدي.",
            color=0x9B59B6)
        embed.add_field(name="🎲 ألعاب", value=(
            "`!ماين <مبلغ> [1-6]` النرد (رقم عشوائي إذا حذفت اختيارك؛ ربح صافٍ x5)\n"
            "`!كنز <مبلغ>` صناديق عشوائية\n`!ابراج <مبلغ>` طوابق عشوائية\n"
            "`!صاروخ <مبلغ>` انفجار عشوائي؛ فوز عند 2x أو أكثر\n"
            "`!سلوتس <مبلغ>` ثلاثة رموز عشوائية\n`!عجلة <مبلغ>` عجلة الحظ\n"
            "`!روليت <مبلغ> أحمر/أسود/أخضر` أو `رقم <0-36>`\n"
            "`!بلاكجاك <مبلغ>` مقارنة ورقتين آلياً، دون سحب أوراق\n"
            "`!سباق <مبلغ> <1-5>` سباق خيل\n`!رماية <مبلغ>` تصويب عشوائي"),
            inline=False)
        embed.add_field(name="📈 اقتصاد ومكافآت", value=(
            "`!استثمار <مبلغ>` مرة كل ساعة\n`!تداول <مبلغ>` كل 30 دقيقة\n"
            "`!راتب` كل 24 ساعة\n`!بخشيش` كل 5 دقائق\n"
            "`!فلوسي` رصيدك\n`!تحويل @user <مبلغ>` تحويل رصيد"), inline=False)
        embed.add_field(name="👥 مشاركة", value=(
            "`!يانصيب [مبلغ]` تذاكر مرجّحة؛ `!يانصيب سحب` للمطور فقط\n"
            "`!تحدي @user <مبلغ>` يتطلب موافقة الطرف الآخر قبل أي خصم"), inline=False)
        embed.set_footer(text="الربح المعروض صافي الزيادة في الرصيد؛ العب بمسؤولية.")
        await ctx.send(embed=embed)

    # Also supply accurate built-in !help entries; the surrounding command menu
    # can discover these commands from their module or the "game" extra.
    usages = {
        "ماين": "!ماين <مبلغ> [رقم 1-6] — الرقم اختياري وعشوائي افتراضياً؛ ربح صافٍ x5 عند المطابقة.",
        "كنز": "!كنز <مبلغ> — ثلاثة صناديق، نتيجة عشوائية.",
        "ابراج": "!ابراج <مبلغ> — حتى خمسة طوابق.",
        "صاروخ": "!صاروخ <مبلغ> — ربح عند وصول المضاعف العشوائي 2x.",
        "استثمار": "!استثمار <مبلغ> — ربح أو خسارة أو تعادل؛ مرة كل ساعة.",
        "تداول": "!تداول <مبلغ> — ربح أو خسارة أو تعادل؛ كل 30 دقيقة.",
        "سلوتس": "!سلوتس <مبلغ> — ثلاثة رموز عشوائية.",
        "عجلة": "!عجلة <مبلغ> — قطاعات عشوائية.",
        "روليت": "!روليت <مبلغ> أحمر/أسود/أخضر أو !روليت <مبلغ> رقم <0-36>.",
        "بلاكجاك": "!بلاكجاك <مبلغ> — مقارنة ورقتين آلياً دون سحب.",
        "سباق": "!سباق <مبلغ> <حصان 1-5> — سباق خيل.",
        "رماية": "!رماية <مبلغ> — تصويب عشوائي.",
        "راتب": "!راتب — 500 إلى 2000 رصيد افتراضي كل 24 ساعة.",
        "بخشيش": "!بخشيش — مكافأة عشوائية كل 5 دقائق، قد تساوي صفراً.",
        "فلوسي": "!فلوسي — معرفة رصيدك الافتراضي.",
        "تحويل": "!تحويل @user <مبلغ> — تحويل رصيد افتراضي إلى عضو.",
        "يانصيب": "!يانصيب [مبلغ] — شراء تذاكر مرجّحة؛ !يانصيب سحب للمطور.",
        "يانصيب_سحب": "!يانصيب_سحب — سحب الجائزة (للمطور فقط).",
        "تحدي": "!تحدي @user <مبلغ> — لا خصم قبل موافقة الطرف الآخر.",
        "كازينو": "!كازينو — عرض الألعاب والمكافآت الافتراضية.",
    }
    for name, help_text in usages.items():
        command = bot.get_command(name)
        command.help = help_text
        command.extras["game"] = True