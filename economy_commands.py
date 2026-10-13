"""Business Empire: land and buildings purchased with virtual credits only.

Register once during bot setup with setup_economy_commands(bot, store).
Income is computed lazily from timestamps, including time spent offline.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime

import discord

from game_store import GameStore, STARTING_MONEY


LAND_PRICE = 1000
BUILDINGS = {
    "kosh": {"name": "🏪 كشك", "price": 500, "income_per_hour": 50, "emoji": "🏪", "max_level": 5},
    "shop": {"name": "🏬 محل", "price": 2000, "income_per_hour": 200, "emoji": "🏬", "max_level": 5},
    "restaurant": {"name": "🍔 مطعم", "price": 5000, "income_per_hour": 600, "emoji": "🍔", "max_level": 5},
    "building": {"name": "🏢 عمارة", "price": 15000, "income_per_hour": 2000, "emoji": "🏢", "max_level": 5},
    "tower": {"name": "🏙️ برج", "price": 50000, "income_per_hour": 8000, "emoji": "🏙️", "max_level": 5},
    "skyscraper": {"name": "🌆 ناطحة سحاب", "price": 200000, "income_per_hour": 40000, "emoji": "🌆", "max_level": 5},
}


class GameManager:
    """Synchronous game operations; every mutation uses the shared store lock."""

    def __init__(self, store: GameStore):
        self.store = store

    def get_user(self, user_id):
        # Create accounts durably, then return an isolated copy for display.
        with self.store.transaction() as data:
            return copy.deepcopy(self.store.user(data, user_id))

    @staticmethod
    def get_land_income(land):
        if land["building"] is None:
            return 0
        base = BUILDINGS[land["building"]]["income_per_hour"]
        # Exact integer equivalent of 1 + (level - 1) * 0.5.
        return base * (land["level"] + 1) // 2

    def get_pending_income(self, land, now=None):
        if land["building"] is None:
            return 0, 0
        now = time.time() if now is None else now
        seconds = max(0, now - land.get("last_collect", now))
        accrued = self.get_land_income(land) * seconds / 3600 + land.get("income_remainder", 0)
        return int(accrued), int(seconds)

    def _settle_land(self, land, now, force=False):
        """Settle using the CURRENT rate; retain fractional virtual credits."""
        pending, _ = self.get_pending_income(land, now)
        if pending or force:
            elapsed = max(0, now - land.get("last_collect", now))
            accrued = self.get_land_income(land) * elapsed / 3600 + land.get("income_remainder", 0)
            land["income_remainder"] = accrued - pending
            land["last_collect"] = max(now, land.get("last_collect", now))
        return pending

    def _collect_user(self, user, now):
        total, details = 0, []
        for number, land in enumerate(user["lands"], 1):
            if land["building"] is None:
                continue
            pending = self._settle_land(land, now)
            if pending > 0:
                total += pending
                details.append(f"• {BUILDINGS[land['building']]['name']} (أرض #{number}): `+{pending:,} عملة`")
        # Developer-granted income follows the same offline accrual rules as
        # buildings. Preserve fractional credit across frequent collections.
        bonus_rate = user.get("bonus_income", 0)
        if bonus_rate > 0:
            elapsed = max(0, now - user.get("bonus_last_collect", now))
            accrued = bonus_rate * elapsed / 3600 + user.get("bonus_remainder", 0)
            bonus = int(accrued)
            user["bonus_remainder"] = accrued - bonus
            user["bonus_last_collect"] = now
            if bonus:
                total += bonus
                details.append(f"• دخل إضافي: `+{bonus:,} عملة`")
        if total:
            user["money"] += total
            user["total_earned"] += total
        return total, details

    def collect_all(self, user_id, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as data:
            return self._collect_user(self.store.user(data, user_id), now)

    def get_total_income_per_hour(self, user_id):
        user = self.get_user(user_id)
        return sum(self.get_land_income(land) for land in user["lands"]) + user.get("bonus_income", 0)

    def buy_land(self, user_id, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as data:
            user = self.store.user(data, user_id)
            if user["money"] < LAND_PRICE:
                return False, f"❌ رصيدك غير كافٍ؛ تحتاج `{LAND_PRICE:,} عملة` لشراء أرض."
            user["money"] -= LAND_PRICE
            user["lands"].append({"building": None, "level": 0, "last_collect": now})
            return True, f"✅ اشتريت أرض جديدة! (أرض #{len(user['lands'])})"

    def build(self, user_id, land_index, building_type, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as data:
            user = self.store.user(data, user_id)
            if type(land_index) is not int or not 1 <= land_index <= len(user["lands"]):
                return False, "❌ رقم الأرض غير صحيح! استخدم !بنك لمعرفة أرقام أراضيك."
            land = user["lands"][land_index - 1]
            if land["building"] is not None:
                return False, "❌ يوجد مبنى على هذه الأرض بالفعل!"
            if building_type not in BUILDINGS:
                return False, "❌ نوع المبنى غير صحيح! استخدم !بناء لعرض الأنواع."
            building = BUILDINGS[building_type]
            if user["money"] < building["price"]:
                return False, f"❌ تحتاج `{building['price']:,} عملة` لبناء {building['name']}!"
            user["money"] -= building["price"]
            land.update(building=building_type, level=1, last_collect=now)
            return True, f"✅ بنيت {building['name']} على أرض #{land_index}!"

    def upgrade(self, user_id, land_index, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as data:
            user = self.store.user(data, user_id)
            if type(land_index) is not int or not 1 <= land_index <= len(user["lands"]):
                return False, "❌ رقم الأرض غير صحيح! استخدم !بنك لمعرفة أرقام أراضيك."
            land = user["lands"][land_index - 1]
            if land["building"] is None:
                return False, "❌ لا يوجد مبنى على هذه الأرض!"
            building = BUILDINGS[land["building"]]
            if land["level"] >= building["max_level"]:
                return False, f"❌ المبنى وصل الحد الأقصى (مستوى {building['max_level']})!"
            # Collect old-rate earnings first, exactly as in the reference.
            self._collect_user(user, now)
            cost = building["price"] * land["level"] * 8 // 10
            if user["money"] < cost:
                return False, f"❌ تحتاج `{cost:,} عملة` للتطوير!"
            # Even when the old-rate earning is <1 credit, close that period
            # before changing rates so elapsed time cannot be repriced upward.
            self._settle_land(land, now, force=True)
            user["money"] -= cost
            land["level"] += 1
            return True, (
                f"✅ تم تطوير {building['name']} إلى مستوى **{land['level']}**!\n"
                f"💰 الدخل الجديد: `{self.get_land_income(land):,} عملة/ساعة`"
            )

    def get_leaderboard(self, limit=10):
        players = [
            {"user_id": uid, "money": user["money"],
             "total_earned": user.get("total_earned", 0), "lands": len(user["lands"])}
            for uid, user in self.store.snapshot()["users"].items()
        ]
        return sorted(players, key=lambda player: player["money"], reverse=True)[:limit]


def _land_number(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def setup_economy_commands(bot, store: GameStore):
    """Synchronously register each command exactly once, never in on_ready."""
    if getattr(bot, "_economy_game", None) is not None:
        return bot._economy_game
    if bot.get_command("بنك") is not None:
        raise ValueError("Cannot register economy commands: !بنك is already registered")
    game = GameManager(store)

    @bot.command(name="بنك", aliases=["bank", "رصيد"])
    async def bank(ctx, page: str = "1"):
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            await ctx.reply("⚠️ الاستخدام: `!بنك` أو `!بنك <رقم الصفحة>`")
            return
        if page_number < 1:
            await ctx.reply("⚠️ رقم الصفحة يجب أن يكون 1 أو أكبر.")
            return
        user = game.get_user(ctx.author.id)
        page_size = 5
        page_count = max(1, (len(user["lands"]) + page_size - 1) // page_size)
        if page_number > page_count:
            await ctx.reply(f"⚠️ الصفحة غير موجودة. عدد الصفحات: {page_count}.")
            return
        embed = discord.Embed(title=f"🏦 بنك {ctx.author.name}", color=0xFFD700, timestamp=datetime.now())
        if ctx.author.avatar:
            embed.set_thumbnail(url=ctx.author.avatar.url)
        embed.add_field(name="💰 الرصيد الحالي", value=f"`{user['money']:,} عملة`")
        embed.add_field(name="📈 إجمالي الأرباح", value=f"`{user['total_earned']:,} عملة`")
        embed.add_field(name="🏞️ عدد الأراضي", value=f"`{len(user['lands'])}`")
        embed.add_field(name="⏰ الدخل/ساعة", value=f"`{sum(game.get_land_income(land) for land in user['lands']) + user.get('bonus_income', 0):,} عملة`")
        if user["lands"]:
            lines = []
            now = time.time()
            start = (page_number - 1) * page_size
            for i, land in enumerate(user["lands"][start:start + page_size], start + 1):
                if land["building"]:
                    building = BUILDINGS[land["building"]]
                    pending, _ = game.get_pending_income(land, now)
                    lines.append(f"**#{i}** {building['name']} (مستوى {land['level']}) - `+{pending:,} عملة` جاهز")
                else:
                    lines.append(f"**#{i}** 🏞️ أرض فاضية")
            embed.add_field(
                name=f"🏞️ أراضيك — صفحة {page_number}/{page_count}",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name="🏞️ أراضيك", value="*ما عندك أراضي! استخدم `!شراء` لشراء أرض*", inline=False)
        embed.set_footer(text=f"استخدم !جمع لجمع الأرباح • للتصفح: !بنك <1-{page_count}> • عملات افتراضية فقط")
        await ctx.send(embed=embed)

    @bot.command(name="شراء", aliases=["buy"])
    async def buy_land(ctx):
        success, message = game.buy_land(ctx.author.id)
        if not success:
            await ctx.reply(message)
            return
        user = game.get_user(ctx.author.id)
        embed = discord.Embed(title="🏞️ أرض جديدة!", description=message, color=0x00FF00)
        embed.add_field(name="💰 الرصيد المتبقي", value=f"`{user['money']:,} عملة`")
        embed.add_field(name="🏞️ أراضيك", value=f"`{len(user['lands'])}`")
        embed.set_footer(text="استخدم !بناء <رقم الأرض> <نوع المبنى>")
        await ctx.send(embed=embed)

    @bot.command(name="بناء", aliases=["build"])
    async def build(ctx, land_number: str = None, building_type: str = None):
        if land_number is None or building_type is None:
            embed = discord.Embed(
                title="🏗️ أنواع المباني المتاحة",
                description="**الاستخدام:** `!بناء <رقم الأرض> <نوع المبنى>`",
                color=0x4A90E2,
            )
            for key, building in BUILDINGS.items():
                embed.add_field(
                    name=building["name"],
                    value=(f"💵 السعر: `{building['price']:,} عملة`\n"
                           f"💰 الدخل: `{building['income_per_hour']:,} عملة/ساعة`\n"
                           f"🔧 الأمر: `!بناء 1 {key}`"),
                )
            embed.set_footer(text="💡 مثال: !بناء 1 shop • أرصدة افتراضية فقط")
            await ctx.send(embed=embed)
            return
        number = _land_number(land_number)
        success, message = game.build(ctx.author.id, number, building_type)
        if not success:
            await ctx.reply(message)
            return
        user = game.get_user(ctx.author.id)
        embed = discord.Embed(title="🏗️ تم البناء!", description=message, color=0x00FF00)
        embed.add_field(name="💰 الرصيد المتبقي", value=f"`{user['money']:,} عملة`")
        embed.add_field(name="📈 دخل المبنى", value=f"`{game.get_land_income(user['lands'][number - 1]):,} عملة/ساعة`")
        await ctx.send(embed=embed)

    @bot.command(name="جمع", aliases=["collect"])
    async def collect(ctx):
        total, details = game.collect_all(ctx.author.id)
        if total == 0:
            await ctx.reply("📭 ما فيه أرباح جاهزة حالياً!\n⏰ انتظر شوي وعاود.")
            return
        user = game.get_user(ctx.author.id)
        embed = discord.Embed(title="💰 تم جمع الأرباح!", description=f"**ربحت `+{total:,} عملة`!** 🎉",
                              color=0x00FF00, timestamp=datetime.now())
        embed.add_field(name="📊 التفاصيل", value="\n".join(details), inline=False)
        embed.add_field(name="💵 رصيدك الآن", value=f"`{user['money']:,} عملة`", inline=False)
        embed.set_footer(text="🎯 استخدم !تطوير لتطوير مباني")
        await ctx.send(embed=embed)

    @bot.command(name="تطوير", aliases=["upgrade"])
    async def upgrade(ctx, land_number: str = None):
        if land_number is None:
            await ctx.reply("⚠️ **الاستخدام:** `!تطوير <رقم الأرض>`\n💡 مثال: `!تطوير 1`")
            return
        success, message = game.upgrade(ctx.author.id, _land_number(land_number))
        if not success:
            await ctx.reply(message)
            return
        user = game.get_user(ctx.author.id)
        embed = discord.Embed(title="📈 تم التطوير!", description=message, color=0x00FF00)
        embed.add_field(name="💰 رصيدك الآن", value=f"`{user['money']:,} عملة`")
        await ctx.send(embed=embed)

    @bot.command(name="اغنى", aliases=["leaderboard", "top"])
    async def leaderboard(ctx):
        top = game.get_leaderboard()
        if not top:
            await ctx.reply("📭 ما فيه لاعبين بعد!")
            return
        embed = discord.Embed(title="🏆 أغنى 10 لاعبين", description="**قائمة الأثرياء** 💎",
                              color=0xFFD700, timestamp=datetime.now())
        medals = ["🥇", "🥈", "🥉"]
        for i, player in enumerate(top, 1):
            try:
                user = await bot.fetch_user(int(player["user_id"]))
                name = discord.utils.escape_markdown(user.name)
            except discord.DiscordException:
                name = f"مستخدم {player['user_id'][:6]}"
            medal = medals[i - 1] if i <= 3 else f"**{i}.**"
            embed.add_field(
                name=f"{medal} {name}",
                value=(f"💰 `{player['money']:,} عملة` • 🏞️ `{player['lands']}` أرض • "
                       f"📈 `{player['total_earned']:,} عملة` أرباح"),
                inline=False,
            )
        await ctx.send(embed=embed)

    @bot.command(name="الالعاب", aliases=["games", "help_game"])
    async def game_help(ctx):
        embed = discord.Embed(
            title="🎮 إمبراطورية الأعمال",
            description=(f"**مرحباً بك في لعبة الاستثمار الافتراضية! 🏙️**\n\n"
                         f"ابنِ إمبراطوريتك العقارية وكن أغنى لاعب!\n"
                          f"💰 رأس مالك الأولي: `{STARTING_MONEY:,} عملة` (رصيد افتراضي فقط، لا مال حقيقي)"),
            color=0x9B59B6,
        )
        for name, value in (
            ("🏞️ `!شراء`", f"اشتري أرض جديدة بـ `{LAND_PRICE:,} عملة`"),
            ("🏗️ `!بناء <رقم> <نوع>`", "ابنِ مبنى على أرضك؛ مثال: `!بناء 1 shop`"),
            ("💰 `!جمع`", "اجمع الأرباح المتراكمة من كل أراضيك، حتى أثناء توقف البوت"),
            ("📈 `!تطوير <رقم>`", "طوّر مبنى لزيادة دخله؛ كل مستوى يزيد 50%، حتى المستوى 5"),
            ("🏦 `!بنك [الصفحة]`", "شوف رصيدك وأراضيك ومعلوماتك؛ مثال: `!بنك 2`"),
            ("🏆 `!اغنى`", "قائمة أغنى 10 لاعبين"),
        ):
            embed.add_field(name=name, value=value, inline=False)
        embed.add_field(
            name="🏢 أنواع المباني",
            value="\n".join(
                f"{building['name']} - `{building['price']:,} عملة` (دخل `{building['income_per_hour']:,} عملة/س`) "
                f"— `{key}`" for key, building in BUILDINGS.items()
            ),
            inline=False,
        )
        embed.set_footer(text="🎯 أرصدة افتراضية فقط • البيانات محلية وقد لا تبقى بعد إعادة النشر")
        await ctx.send(embed=embed)

    bot._economy_game = game
    usages = {
        "بنك": "!بنك [الصفحة] — عرض الرصيد والأراضي والدخل.",
        "شراء": "!شراء — شراء أرض جديدة مقابل عملات افتراضية.",
        "بناء": "!بناء <رقم الأرض> <نوع المبنى> — اكتب !بناء لعرض الأنواع.",
        "جمع": "!جمع — جمع دخل المباني المتراكم.",
        "تطوير": "!تطوير <رقم الأرض> — تطوير المبنى حتى المستوى الخامس.",
        "اغنى": "!اغنى — عرض أغنى 10 لاعبين.",
        "الالعاب": "!الالعاب — عرض دليل الاقتصاد والمباني.",
    }
    for name, help_text in usages.items():
        command = bot.get_command(name)
        command.help = help_text
        command.extras["game"] = True
    return game