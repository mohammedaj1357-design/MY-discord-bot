"""نظام الأصدقاء."""
import json
from pathlib import Path
import discord
from discord.ext import commands

FRIENDS_FILE = "data/friends.json"

def load_friends():
    file = Path(FRIENDS_FILE)
    file.parent.mkdir(parents=True, exist_ok=True)
    if not file.exists():
        return {}
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_friends(data):
    with open(FRIENDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_user_friends(user_id, guild_id):
    data = load_friends()
    key = f"{guild_id}_{user_id}"
    if key not in data:
        data[key] = {"friends": [], "bestie": None}
        save_friends(data)
    return data[key]


def setup_friends(bot, developer_id):
    """تسجيل أوامر الأصدقاء."""
    
    @bot.command(name="friend", aliases=["صديق"])
    async def friend_add(ctx, user: discord.Member = None):
        """إضافة صديق."""
        if user is None:
            await ctx.reply("⚠️ **الاستخدام:** `!friend @user`")
            return
        if user.id == ctx.author.id:
            await ctx.reply("⚠️ ما تقدر تضيف نفسك!")
            return
        if user.bot:
            await ctx.reply("⚠️ ما تقدر تضيف بوت!")
            return
        
        data = load_friends()
        my_key = f"{ctx.guild.id}_{ctx.author.id}"
        their_key = f"{ctx.guild.id}_{user.id}"
        
        if my_key not in data:
            data[my_key] = {"friends": [], "bestie": None}
        if their_key not in data:
            data[their_key] = {"friends": [], "bestie": None}
        
        if user.id in data[my_key]["friends"]:
            await ctx.reply(f"⚠️ {user.mention} صديقك أصلاً!")
            return
        
        data[my_key]["friends"].append(user.id)
        data[their_key]["friends"].append(ctx.author.id)
        save_friends(data)
        
        embed = discord.Embed(
            title="👥 صديق جديد!",
            description=f"{ctx.author.mention} و {user.mention} صاروا أصدقاء! 🎉",
            color=0x00FF00
        )
        await ctx.send(embed=embed)
    
    @bot.command(name="unfriend", aliases=["الغاء_صداقة"])
    async def unfriend(ctx, user: discord.Member = None):
        """إزالة صديق."""
        if user is None:
            await ctx.reply("⚠️ **الاستخدام:** `!unfriend @user`")
            return
        
        data = load_friends()
        my_key = f"{ctx.guild.id}_{ctx.author.id}"
        their_key = f"{ctx.guild.id}_{user.id}"
        
        if my_key not in data or user.id not in data[my_key]["friends"]:
            await ctx.reply(f"⚠️ {user.mention} مو صديقك!")
            return
        
        data[my_key]["friends"].remove(user.id)
        if their_key in data and ctx.author.id in data[their_key]["friends"]:
            data[their_key]["friends"].remove(ctx.author.id)
        save_friends(data)
        await ctx.reply(f"❌ تم إزالة {user.mention} من أصدقائك")
    
    @bot.command(name="friends", aliases=["اصدقائي"])
    async def friends_list(ctx, user: discord.Member = None):
        """عرض قائمة الأصدقاء."""
        if user is None:
            user = ctx.author
        
        data = get_user_friends(user.id, ctx.guild.id)
        
        if not data["friends"]:
            await ctx.reply(f"📭 {user.mention} ما عنده أصدقاء بعد!")
            return
        
        embed = discord.Embed(
            title=f"👥 أصدقاء {user.name}",
            description=f"**العدد:** `{len(data['friends'])}`",
            color=0x4A90E2
        )
        
        # أعرض آخر 15 صديق
        for i, fid in enumerate(data["friends"][:15], 1):
            member = ctx.guild.get_member(fid)
            if member:
                bestie = "⭐" if fid == data["bestie"] else ""
                embed.add_field(
                    name=f"{i}. {member.name} {bestie}",
                    value=member.mention,
                    inline=True
                )
        
        if len(data["friends"]) > 15:
            embed.set_footer(text=f"+{len(data['friends']) - 15} صديق آخر")
        
        await ctx.send(embed=embed)
    
    @bot.command(name="bestie", aliases=["اعز_صديق"])
    async def bestie(ctx, user: discord.Member = None):
        """تحديد أعز صديق."""
        if user is None:
            await ctx.reply("⚠️ **الاستخدام:** `!bestie @user`")
            return
        
        data = load_friends()
        my_key = f"{ctx.guild.id}_{ctx.author.id}"
        
        if my_key not in data or user.id not in data[my_key]["friends"]:
            await ctx.reply(f"⚠️ {user.mention} مو صديقك!")
            return
        
        data[my_key]["bestie"] = user.id
        save_friends(data)
        
        embed = discord.Embed(
            title="⭐ أعز صديق!",
            description=f"{ctx.author.mention} اختار {user.mention} كأعز صديق! ❤️",
            color=0xFF69B4
        )
        await ctx.send(embed=embed)
    
    print("✅ تم تحميل نظام الأصدقاء")
    return bot