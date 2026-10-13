"""نظام المتجر."""
import json
from pathlib import Path
import discord
from discord.ext import commands
from datetime import datetime

SHOP_FILE = "data/shop_data.json"
INVENTORY_FILE = "data/inventory.json"

SHOP_ITEMS = {
    "vip": {"name": "👑 رتبة VIP", "price": 50000, "type": "role", "emoji": "👑"},
    "vip_plus": {"name": "💎 رتبة VIP+", "price": 100000, "type": "role", "emoji": "💎"},
    "legend": {"name": "🏆 رتبة أسطورة", "price": 500000, "type": "role", "emoji": "🏆"},
    "lucky_box": {"name": "🎁 صندوق حظ", "price": 5000, "type": "box", "emoji": "🎁"},
    "mega_box": {"name": "💝 صندوق ضخم", "price": 20000, "type": "box", "emoji": "💝"},
    "title_king": {"name": "👑 لقب: الملك", "price": 30000, "type": "title", "emoji": "👑"},
    "title_pro": {"name": "⚡ لقب: المحترف", "price": 15000, "type": "title", "emoji": "⚡"},
    "badge_gold": {"name": "🥇 شارة ذهبية", "price": 25000, "type": "badge", "emoji": "🥇"},
}

def load_json(file, default=None):
    f = Path(file)
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        return default or {}
    try:
        with open(f, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except:
        return default or {}

def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def setup_shop(bot, developer_id, game_store):
    """تسجيل أوامر المتجر."""
    
    @bot.command(name="shop", aliases=["متجر"])
    async def shop_cmd(ctx):
        """عرض المتجر."""
        embed = discord.Embed(
            title="🛒 المتجر",
            description="اشترِ بالفلوس الافتراضية من `!بنك`\n**الاستخدام:** `!buy <id>`",
            color=0xFFD700
        )
        
        for item_id, item in SHOP_ITEMS.items():
            embed.add_field(
                name=f"{item['emoji']} {item['name']}",
                value=f"💰 `{item['price']:,}` عملة\n🆔 `{item_id}`",
                inline=True
            )
        
        embed.set_footer(text="استخدم !inventory لعرض مخزونك")
        await ctx.send(embed=embed)
    
    @bot.command(name="purchase", aliases=["اشتري", "shopbuy"])
    async def buy_cmd(ctx, item_id: str = None):
        """شراء عنصر."""
        if not item_id:
            await ctx.reply("⚠️ **الاستخدام:** `!buy <id>`\nشوف `!shop`")
            return
        
        item_id = item_id.lower()
        if item_id not in SHOP_ITEMS:
            await ctx.reply("⚠️ العنصر مو موجود! شوف `!shop`")
            return
        
        item = SHOP_ITEMS[item_id]
        
        # جيب رصيد المستخدم
        try:
            with game_store.transaction() as data:
                uid = str(ctx.author.id)
                if uid not in data.get("users", {}):
                    data.setdefault("users", {})[uid] = {
                        "money": 5000, "lands": [], "total_earned": 0,
                        "created_at": datetime.now().isoformat()
                    }
                user = data["users"][uid]
                
                if user["money"] < item["price"]:
                    await ctx.reply(f"❌ ما عندك فلوس كافية!\n💰 رصيدك: `{user['money']:,}`\n💵 المطلوب: `{item['price']:,}`")
                    return
                
                user["money"] -= item["price"]
        except Exception as e:
            await ctx.reply(f"⚠️ خطأ: {e}")
            return
        
        # أضف للمخزون
        inv = load_json(INVENTORY_FILE, {})
        key = f"{ctx.guild.id}_{ctx.author.id}"
        if key not in inv:
            inv[key] = []
        inv[key].append({
            "item_id": item_id,
            "name": item["name"],
            "bought_at": datetime.now().isoformat()
        })
        save_json(INVENTORY_FILE, inv)
        
        embed = discord.Embed(
            title="✅ تم الشراء!",
            description=f"{item['emoji']} **{item['name']}**",
            color=0x00FF00
        )
        embed.add_field(name="💰 السعر", value=f"`{item['price']:,}`", inline=True)
        embed.add_field(name="🎒 المخزون", value=f"`{len(inv[key])}` عنصر", inline=True)
        await ctx.send(embed=embed)
    
    @bot.command(name="inventory", aliases=["مخزوني", "حقيبتي"])
    async def inventory_cmd(ctx, user: discord.Member = None):
        """عرض المخزون."""
        if user is None:
            user = ctx.author
        
        inv = load_json(INVENTORY_FILE, {})
        key = f"{ctx.guild.id}_{user.id}"
        items = inv.get(key, [])
        
        if not items:
            await ctx.reply(f"🎒 {user.mention} ما عنده شيء!")
            return
        
        embed = discord.Embed(
            title=f"🎒 مخزون {user.name}",
            description=f"**العدد:** `{len(items)}`",
            color=0x9B59B6
        )
        
        # جمّع العناصر المتشابهة
        counts = {}
        for item in items:
            counts[item["item_id"]] = counts.get(item["item_id"], 0) + 1
        
        for item_id, count in counts.items():
            if item_id in SHOP_ITEMS:
                item = SHOP_ITEMS[item_id]
                embed.add_field(
                    name=f"{item['emoji']} {item['name']}",
                    value=f"العدد: `{count}`",
                    inline=True
                )
        
        await ctx.send(embed=embed)
    
    @bot.command(name="openbox", aliases=["افتح_صندوق"])
    async def open_box(ctx, box_type: str = None):
        """افتح صندوق حظ."""
        import random
        
        if not box_type:
            await ctx.reply("⚠️ **الاستخدام:** `!openbox lucky` أو `!openbox mega`")
            return
        
        inv = load_json(INVENTORY_FILE, {})
        key = f"{ctx.guild.id}_{ctx.author.id}"
        items = inv.get(key, [])
        
        # ابحث عن الصندوق
        box_id = "lucky_box" if box_type == "lucky" else "mega_box" if box_type == "mega" else None
        if not box_id:
            await ctx.reply("⚠️ نوع الصندوق غلط! (`lucky` أو `mega`)")
            return
        
        found = None
        for i, item in enumerate(items):
            if item["item_id"] == box_id:
                found = i
                break
        
        if found is None:
            await ctx.reply(f"⚠️ ما عندك `{box_type}`!")
            return
        
        # احذف الصندوق
        items.pop(found)
        inv[key] = items
        save_json(INVENTORY_FILE, inv)
        
        # الجوايز
        prizes = [
            (5000, "💰 5,000 عملة"),
            (10000, "💰 10,000 عملة"),
            (25000, "💰 25,000 عملة"),
            (50000, "💎 50,000 عملة"),
            (100000, "👑 100,000 عملة"),
        ]
        
        prize, prize_text = random.choice(prizes)
        
        # أضف الفلوس
        try:
            with game_store.transaction() as data:
                uid = str(ctx.author.id)
                data["users"][uid]["money"] += prize
        except:
            pass
        
        embed = discord.Embed(
            title="🎁 فتحت الصندوق!",
            description=f"**ربحت:** {prize_text}",
            color=0xFFD700
        )
        await ctx.send(embed=embed)
    
    print("✅ تم تحميل نظام المتجر")
    return bot