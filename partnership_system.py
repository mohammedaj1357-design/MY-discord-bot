"""نظام الشراكة بين السيرفرات."""
import json
from pathlib import Path
import discord
from discord.ext import commands
from datetime import datetime

PARTNERS_FILE = "data/partnerships.json"

def load_partners():
    file = Path(PARTNERS_FILE)
    file.parent.mkdir(parents=True, exist_ok=True)
    if not file.exists():
        return {}
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_partners(data):
    with open(PARTNERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def setup_partnership(bot, developer_id):
    """تسجيل أوامر الشراكة."""
    
    @bot.command(name="partner", aliases=["شراكة"])
    async def partner_cmd(ctx, action: str = None, *, args: str = None):
        """إدارة الشراكة بين السيرفرات."""
        if ctx.author.id != developer_id:
            await ctx.reply("⚠️ **للمطور فقط!**")
            return
        
        data = load_partners()
        gid = str(ctx.guild.id)
        
        if gid not in data:
            data[gid] = {"partners": [], "created": datetime.now().isoformat()}
        
        if action == "add":
            # !partner add <guild_id> <اسم السيرفر>
            if not args:
                await ctx.reply("⚠️ **الاستخدام:** `!partner add <ID> <اسم>`")
                return
            
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                await ctx.reply("⚠️ **الاستخدام:** `!partner add <ID> <اسم>`")
                return
            
            try:
                partner_id = int(parts[0])
                partner_name = parts[1]
            except:
                await ctx.reply("⚠️ ID غلط!")
                return
            
            # تحقق إن السيرفر موجود
            partner_guild = bot.get_guild(partner_id)
            if not partner_guild:
                await ctx.reply(f"⚠️ البوت مو في السيرفر `{partner_id}`!")
                return
            
            # تحقق إن ما هو مضاف
            for p in data[gid]["partners"]:
                if p["id"] == partner_id:
                    await ctx.reply("⚠️ السيرفر شريك أصلاً!")
                    return
            
            data[gid]["partners"].append({
                "id": partner_id,
                "name": partner_name,
                "added": datetime.now().isoformat()
            })
            save_partners(data)
            
            embed = discord.Embed(
                title="🤝 شراكة جديدة!",
                description=f"**{ctx.guild.name}** ✕ **{partner_name}**",
                color=0x00FF00
            )
            await ctx.send(embed=embed)
        
        elif action == "remove":
            if not args:
                await ctx.reply("⚠️ **الاستخدام:** `!partner remove <ID>`")
                return
            try:
                partner_id = int(args)
            except:
                await ctx.reply("⚠️ ID غلط!")
                return
            
            for p in data[gid]["partners"]:
                if p["id"] == partner_id:
                    data[gid]["partners"].remove(p)
                    save_partners(data)
                    await ctx.reply(f"❌ تم إزالة الشراكة")
                    return
            
            await ctx.reply("⚠️ الشراكة مو موجودة!")
        
        elif action == "list":
            if not data[gid]["partners"]:
                await ctx.reply("📭 ما فيه شراكات!")
                return
            
            embed = discord.Embed(
                title="🤝 الشراكات",
                description=f"**{ctx.guild.name}**",
                color=0x4A90E2
            )
            for i, p in enumerate(data[gid]["partners"], 1):
                embed.add_field(
                    name=f"{i}. {p['name']}",
                    value=f"🆔 `{p['id']}`",
                    inline=False
                )
            await ctx.send(embed=embed)
        
        elif action == "announce":
            # !partner announce <رسالة>
            if not args:
                await ctx.reply("⚠️ **الاستخدام:** `!partner announce <رسالة>`")
                return
            
            success = 0
            failed = 0
            for p in data[gid]["partners"]:
                partner_guild = bot.get_guild(p["id"])
                if not partner_guild:
                    failed += 1
                    continue
                
                # ابحث عن قناة عامة
                channel = partner_guild.system_channel
                if not channel:
                    for ch in partner_guild.text_channels:
                        if ch.permissions_for(partner_guild.me).send_messages:
                            channel = ch
                            break
                
                if channel:
                    try:
                        embed = discord.Embed(
                            title="📢 إعلان من شريك",
                            description=args,
                            color=0xFFD700
                        )
                        embed.set_footer(text=f"من: {ctx.guild.name}")
                        await channel.send(embed=embed)
                        success += 1
                    except:
                        failed += 1
                else:
                    failed += 1
            
            await ctx.reply(f"✅ نجح: `{success}` • ❌ فشل: `{failed}`")
        
        else:
            embed = discord.Embed(
                title="🤝 نظام الشراكة",
                description="**الأوامر:**",
                color=0x4A90E2
            )
            embed.add_field(name="`!partner add <ID> <اسم>`", value="إضافة شريك", inline=False)
            embed.add_field(name="`!partner remove <ID>`", value="إزالة شريك", inline=False)
            embed.add_field(name="`!partner list`", value="قائمة الشراكات", inline=False)
            embed.add_field(name="`!partner announce <رسالة>`", value="إعلان مشترك", inline=False)
            await ctx.send(embed=embed)
    
    print("✅ تم تحميل نظام الشراكة")
    return bot