"""نظام الزواج."""
import json
from pathlib import Path
from datetime import datetime
import discord
from discord.ext import commands

MARRIAGE_FILE = "data/marriages.json"
PROPOSALS_FILE = "data/proposals.json"

def load_data(file):
    f = Path(file)
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        return {}
    try:
        with open(f, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except:
        return {}

def save_data(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def setup_marriage(bot, developer_id):
    """تسجيل أوامر الزواج."""
    
    @bot.command(name="marry", aliases=["زواج", "تزوج"])
    async def marry_cmd(ctx, user: discord.Member = None):
        """التقدم للزواج من عضو."""
        if user is None:
            await ctx.reply("⚠️ **الاستخدام:** `!marry @user`")
            return
        if user.id == ctx.author.id:
            await ctx.reply("⚠️ ما تقدر تتزوج نفسك! 😂")
            return
        if user.bot:
            await ctx.reply("⚠️ ما تقدر تتزوج بوت! 🤖")
            return
        
        # تحقق إنك مو متزوج
        marriages = load_data(MARRIAGE_FILE)
        gid = str(ctx.guild.id)
        if gid not in marriages:
            marriages[gid] = {}
        
        if str(ctx.author.id) in marriages[gid]:
            partner_id = marriages[gid][str(ctx.author.id)]["partner"]
            partner = ctx.guild.get_member(partner_id)
            await ctx.reply(f"⚠️ أنت متزوج أصلاً من {partner.mention if partner else 'شخص'}! 💍")
            return
        
        # تحقق إن الطرف الثاني مو متزوج
        if str(user.id) in marriages[gid]:
            partner_id = marriages[gid][str(user.id)]["partner"]
            partner = ctx.guild.get_member(partner_id)
            await ctx.reply(f"⚠️ {user.mention} متزوج من {partner.mention if partner else 'شخص'}!")
            return
        
        # احفظ الطلب
        proposals = load_data(PROPOSALS_FILE)
        if gid not in proposals:
            proposals[gid] = {}
        
        proposals[gid][str(user.id)] = {
            "from": ctx.author.id,
            "time": datetime.now().isoformat()
        }
        save_data(PROPOSALS_FILE, proposals)
        
        embed = discord.Embed(
            title="💍 طلب زواج!",
            description=(
                f"{user.mention}\n\n"
                f"**{ctx.author.mention}** يطلب يدك للزواج! 💕\n\n"
                f"اكتب `!accept` للقبول\n"
                f"اكتب `!reject` للرفض\n\n"
                f"⏰ الطلب ينتهي بعد 5 دقائق"
            ),
            color=0xFF69B4
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"من: {ctx.author.name}")
        
        await ctx.send(embed=embed)
    
    @bot.command(name="accept", aliases=["اقبل", "موافق"])
    async def accept_cmd(ctx):
        """قبول طلب الزواج."""
        gid = str(ctx.guild.id)
        proposals = load_data(PROPOSALS_FILE)
        
        if gid not in proposals or str(ctx.author.id) not in proposals[gid]:
            await ctx.reply("📭 ما عندك طلب زواج حالياً!")
            return
        
        proposal = proposals[gid][str(ctx.author.id)]
        proposer_id = proposal["from"]
        proposer = ctx.guild.get_member(proposer_id)
        
        # تحقق من الوقت (5 دقائق)
        prop_time = datetime.fromisoformat(proposal["time"])
        diff = (datetime.now() - prop_time).total_seconds()
        if diff > 300:
            del proposals[gid][str(ctx.author.id)]
            save_data(PROPOSALS_FILE, proposals)
            await ctx.reply("⏰ **انتهى وقت الطلب!**")
            return
        
        # احذف الطلب
        del proposals[gid][str(ctx.author.id)]
        save_data(PROPOSALS_FILE, proposals)
        
        # احفظ الزواج
        marriages = load_data(MARRIAGE_FILE)
        if gid not in marriages:
            marriages[gid] = {}
        
        wedding_date = datetime.now().isoformat()
        marriages[gid][str(ctx.author.id)] = {
            "partner": proposer_id,
            "since": wedding_date
        }
        marriages[gid][str(proposer_id)] = {
            "partner": ctx.author.id,
            "since": wedding_date
        }
        save_data(MARRIAGE_FILE, marriages)
        
        embed = discord.Embed(
            title="💍 مبروووك!",
            description=(
                f"🎉 **{proposer.mention if proposer else 'الشخص'}** و **{ctx.author.mention}**\n"
                f"صاروا **متزوجين**! 💕\n\n"
                f"❤️ ألف مبروك للثنائي الجميل!"
            ),
            color=0xFF69B4
        )
        embed.set_footer(text=f"💒 {datetime.now().strftime('%Y-%m-%d')}")
        
        await ctx.send(embed=embed)
    
    @bot.command(name="reject", aliases=["ارفض", "رفض"])
    async def reject_cmd(ctx):
        """رفض طلب الزواج."""
        gid = str(ctx.guild.id)
        proposals = load_data(PROPOSALS_FILE)
        
        if gid not in proposals or str(ctx.author.id) not in proposals[gid]:
            await ctx.reply("📭 ما عندك طلب زواج!")
            return
        
        proposal = proposals[gid][str(ctx.author.id)]
        proposer = ctx.guild.get_member(proposal["from"])
        
        del proposals[gid][str(ctx.author.id)]
        save_data(PROPOSALS_FILE, proposals)
        
        embed = discord.Embed(
            title="💔 رفض",
            description=f"{ctx.author.mention} رفض طلب {proposer.mention if proposer else 'الشخص'}...",
            color=0xFF0000
        )
        await ctx.send(embed=embed)
    
    @bot.command(name="divorce", aliases=["طلاق"])
    async def divorce_cmd(ctx):
        """طلب الطلاق."""
        gid = str(ctx.guild.id)
        marriages = load_data(MARRIAGE_FILE)
        
        if gid not in marriages or str(ctx.author.id) not in marriages[gid]:
            await ctx.reply("⚠️ أنت مو متزوج!")
            return
        
        partner_id = marriages[gid][str(ctx.author.id)]["partner"]
        partner = ctx.guild.get_member(partner_id)
        
        # احذف الطرفين
        del marriages[gid][str(ctx.author.id)]
        if str(partner_id) in marriages[gid]:
            del marriages[gid][str(partner_id)]
        save_data(MARRIAGE_FILE, marriages)
        
        embed = discord.Embed(
            title="💔 طلاق",
            description=(
                f"{ctx.author.mention} طلق {partner.mention if partner else 'شخص'}...\n\n"
                f"😢 الله يعوضكم"
            ),
            color=0xFF0000
        )
        await ctx.send(embed=embed)
    
    @bot.command(name="mypartner", aliases=["زوجي", "زوجتي", "شريكي"])
    async def my_partner_cmd(ctx, user: discord.Member = None):
        """عرض الزوج/الزوجة."""
        if user is None:
            user = ctx.author
        
        gid = str(ctx.guild.id)
        marriages = load_data(MARRIAGE_FILE)
        
        if gid not in marriages or str(user.id) not in marriages[gid]:
            await ctx.reply(f"💔 {user.mention} مو متزوج!")
            return
        
        info = marriages[gid][str(user.id)]
        partner = ctx.guild.get_member(info["partner"])
        
        since = datetime.fromisoformat(info["since"])
        days = (datetime.now() - since).days
        
        embed = discord.Embed(
            title="💑 الزواج",
            description=f"**{user.mention}** ❤️ **{partner.mention if partner else 'شخص'}**",
            color=0xFF69B4
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(
            name="📅 تاريخ الزواج",
            value=f"`{since.strftime('%Y-%m-%d')}`",
            inline=True
        )
        embed.add_field(
            name="⏰ المدة",
            value=f"`{days}` يوم",
            inline=True
        )
        embed.set_footer(text="💕 الله يديم المحبة")
        
        await ctx.send(embed=embed)
    
    @bot.command(name="marriages", aliases=["المتزوجين"])
    async def marriages_list(ctx):
        """قائمة المتزوجين في السيرفر."""
        gid = str(ctx.guild.id)
        marriages = load_data(MARRIAGE_FILE)
        
        if gid not in marriages or not marriages[gid]:
            await ctx.reply("📭 ما فيه متزوجين في السيرفر!")
            return
        
        embed = discord.Embed(
            title="💑 المتزوجين",
            description=f"في سيرفر **{ctx.guild.name}**",
            color=0xFF69B4
        )
        
        # جمّع الأزواج (بدون تكرار)
        seen = set()
        couples = []
        for uid, data in marriages[gid].items():
            partner_id = data["partner"]
            pair = tuple(sorted([int(uid), partner_id]))
            if pair not in seen:
                seen.add(pair)
                couples.append((uid, partner_id, data["since"]))
        
        for i, (uid1, uid2, since) in enumerate(couples[:15], 1):
            m1 = ctx.guild.get_member(int(uid1))
            m2 = ctx.guild.get_member(uid2)
            
            if m1 and m2:
                date = datetime.fromisoformat(since).strftime("%Y-%m-%d")
                embed.add_field(
                    name=f"{i}. 💕 {m1.name} ❤️ {m2.name}",
                    value=f"📅 `{date}`",
                    inline=False
                )
        
        if len(couples) > 15:
            embed.set_footer(text=f"+{len(couples) - 15} زواج آخر")
        
        await ctx.send(embed=embed)
    
    print("✅ تم تحميل نظام الزواج")
    return bot