"""Discord command browser with premium UI design."""

import logging

import discord

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════
# 🎨 إعدادات التصميم
# ═══════════════════════════════════════
CATEGORY_META = {
    "general": {
        "title": "الأوامر العامة",
        "emoji": "👤",
        "color": 0x3498DB,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/1077/1077114.png",
        "description": "أوامر متاحة لجميع الأعضاء في السيرفر.",
    },
    "emotions": {
        "title": "المشاعر والعلاقة",
        "emoji": "💕",
        "color": 0xFF69B4,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/833/833472.png",
        "description": "ملف عاطفي اختياري — خاص بكل عضو.",
    },
    "admin": {
        "title": "أوامر الإدارة",
        "emoji": "🛡️",
        "color": 0xFFA500,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/3062/3062634.png",
        "description": "أوامر للمشرفين والإداريين.",
    },
    "dev": {
        "title": "أوامر المطور",
        "emoji": "👑",
        "color": 0xFFD700,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/2991/2991148.png",
        "description": "أوامر خاصة بمطور البوت فقط.",
    },
    "ai": {
        "title": "الذكاء الاصطناعي",
        "emoji": "🤖",
        "color": 0x9B59B6,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/2103/2103633.png",
        "description": "طريقة استخدام البوت للمحادثة والمساعدة.",
    },
    "economy": {
        "title": "الأراضي والاقتصاد",
        "emoji": "💰",
        "color": 0x27AE60,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/2489/2489756.png",
        "description": "شراء أراضٍ، بناء، تطوير، وجمع الأرباح.",
    },
    "casino": {
        "title": "الألعاب والمكافآت",
        "emoji": "🎰",
        "color": 0xE67E22,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/2662/2662503.png",
        "description": "ألعاب افتراضية ومكافآت يومية.",
    },
    "social": {
        "title": "الزواج والأصدقاء",
        "emoji": "💍",
        "color": 0xFF1493,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/1077/1077035.png",
        "description": "تفاعل اجتماعي مع الأعضاء الآخرين.",
    },
    "shop": {
        "title": "المتجر",
        "emoji": "🛒",
        "color": 0x9B59B6,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/1170/1170576.png",
        "description": "اشترِ رتب وألقاب وشارات بالفلوس الافتراضية.",
    },
    "utility": {
        "title": "أدوات مفيدة",
        "emoji": "🌤️",
        "color": 0x1ABC9C,
        "thumbnail": "https://cdn-icons-png.flaticon.com/512/1163/1163624.png",
        "description": "أدوات مفيدة — طقس، توقعات، والمزيد.",
    },
}


# ═══════════════════════════════════════
# 📋 الأوامر
# ═══════════════════════════════════════
COMMANDS = {
    "general": [
        ("!about", "معلومات عن البوت", "ℹ️"),
        ("!ping", "قياس زمن استجابة Discord", "🏓"),
        ("!uptime", "مدة تشغيل نسخة البوت الحالية", "⏱️"),
        ("!memory", "عدد الرسائل المؤقتة في سياقك", "🧠"),
        ("!server", "عرض معلومات السيرفر الحالي", "📊"),
        ("!whereami", "معرفة السيرفر والقناة الحالية", "📍"),
        ("!رابط", "إنشاء أو عرض دعوة دائمة للسيرفر", "🔗"),
        ("!احصائيات_دعوات", "عدد الدعوات واستخداماتها", "📈"),
        ("!احذف_دعواتي", "حذف الدعوات التي أنشأتها", "🗑️"),
        ("!مستواي", "عرض مستوى XP وتقدّمك", "📊"),
        ("!توب", "أفضل 10 أعضاء حسب XP", "🏆"),
        ("!xp_help", "شرح كسب XP من الرسائل والصوت", "❓"),
        ("!level_roles", "عرض مكافآت الرتب المضبوطة", "🎭"),
        ("!image", "تعديل صورة مرفقة", "🖼️"),
        ("!play", "تشغيل أغنية أو إضافتها للقائمة", "🎵"),
        ("!queue", "عرض قائمة انتظار الموسيقى", "📋"),
        ("!nowplaying", "الأغنية التي تعمل حاليًا", "🎶"),
        ("!command", "عرض قائمة الأوامر التفاعلية", "📖"),
        ("!help", "عرض المساعدة النصية", "❓"),
        ("!currentchannel", "عرض قناة ردود البوت المحددة", "📺"),
        ("!updates", "عرض آخر إصدارات البوت المسجلة", "🆕"),
        ("!تعلم", "التحقق من حالة التعلّم الاختياري", "🧠"),
        ("!معلوماتي", "إرسال ملف بياناتك إليك", "📄"),
        ("!نسيان", "حذف معلومات أو اهتمامات مطابقة", "🗑️"),
        ("!مسح_معلوماتي", "حذف ملفك كله وإيقاف التعلّم", "❌"),
    ],
    "emotions": [
        ("!مشاعر", "شرح ملف المشاعر المحاكى وحالة موافقتك", "💕"),
        ("!مشاعر_تشغيل", "الموافقة على تحليل الرسائل", "✅"),
        ("!مشاعر_ايقاف", "إيقاف التحليل", "⛔"),
        ("!مشاعري", "إرسال حالتك وعلاقتك المحاكاة", "😊"),
        ("!علاقتنا", "عرض مستوى العلاقة والألقاب", "💑"),
        ("!لقب", "حفظ لقب اخترته لنفسك", "🏷️"),
        ("!نكتة", "حفظ نكتة داخلية", "😂"),
        ("!احب", "حفظ مواضيع تهمك", "❤️"),
        ("!مااحب", "حفظ مواضيع تفضّل تجنبها", "💔"),
        ("!اعتذر", "تحديث ملف الحالة بعد الاعتذار", "🙏"),
        ("!مسح_مشاعري", "حذف ملف المشاعر كله", "🗑️"),
    ],
    "admin": [
        ("!level_role_set", "ضبط رتبة مكافأة عند بلوغ مستوى", "🎭"),
        ("!level_role_remove", "حذف مكافأة رتبة لمستوى", "🗑️"),
        ("/levelsetup", "تحديد روم إشعارات الارتقاء", "⚙️"),
        ("!addxp", "منح XP لعضو", "⚡"),
        ("!resetxp", "طلب تصفير سجل المستوى", "🔄"),
        ("!setchannel", "تحديد قناة ردود البوت", "📺"),
        ("!clearchannel", "إلغاء التحديد", "🔓"),
        ("/set image", "تغيير صورة الردود", "🖼️"),
        ("/set name", "تحديد اسم مخصص للردود", "📝"),
        ("/set show", "معاينة الإعدادات الحالية", "👁️"),
        ("/antilink", "منع الروابط مع حذف ومهلة", "🔗"),
        ("/ban", "حظر عضو", "🚫"),
        ("/kick", "طرد عضو", "👢"),
        ("/timeout", "إسكات عضو مؤقتاً", "🔇"),
        ("/unmute", "فك الإسكات", "🔊"),
        ("/warn", "تحذير عضو", "⚠️"),
        ("/purge", "حذف رسائل", "🗑️"),
        ("/slowmode", "بطء القناة", "🐌"),
        ("/memberinfo", "معلومات عضو", "📋"),
        ("/role", "إضافة رتبة أو إزالتها", "🎭"),
        ("/autorole", "إدارة رتب الأعضاء الجدد", "⚙️"),
        ("/keyword", "تحديد الرتب المسموح لها", "🎯"),
        ("/shortcuts", "عرض اختصارات الإشراف", "⌨️"),
    ],
    "dev": [
        ("!adddev", "إضافة مطور مساعد", "👑"),
        ("!removedev", "إزالة مطور مساعد", "❌"),
        ("!devlist", "قائمة المطورين المساعدين", "📋"),
        ("!dev", "معلومات المطور", "👑"),
        ("/owner", "إدارة الأونرز المساعدين", "👑"),
        ("/send_weekly_stats", "ملخص إجراءات الإشراف لآخر 7 أيام", "📊"),
        ("/maintenance", "تشغيل أو إيقاف الصيانة", "🔧"),
        ("/update", "تسجيل إصدار جديد", "📢"),
        ("!partner add", "إضافة شريك للسيرفر", "🤝"),
        ("!partner remove", "إزالة شريك", "❌"),
        ("!partner list", "قائمة الشراكات", "📋"),
        ("!partner announce", "إعلان مشترك لكل الشراكات", "📢"),
        ("!dm", "رسالة خاصة مع مرفقات", "📩"),
        ("!dm everyone", "إرسال للمشتركين في كل السيرفرات", "📢"),
        ("!يانصيب_سحب", "سحب فائز باليانصيب", "🎟️"),
        ("!adminmenu", "دليل كل أوامر المطور", "📖"),
        ("!addmoney", "إضافة فلوس للاعب", "💵"),
        ("!removemoney", "خصم فلوس من لاعب", "💸"),
        ("!setmoney", "تحديد رصيد لاعب", "🎯"),
        ("!givemoney", "منح رصيد لكل اللاعبين", "🎁"),
        ("!addlands", "إضافة أراضٍ", "🏞️"),
        ("!addincome", "إضافة دخل/ساعة", "📈"),
        ("!block", "حظر لاعب من الألعاب", "🚫"),
        ("!unblock", "إلغاء حظر لاعب", "✅"),
        ("!blocked", "قائمة المحظورين", "📋"),
        ("!stats", "إحصائيات اللعبة", "📊"),
        ("!moneyboard", "اقتصاد اللعبة", "💰"),
        ("!allplayers", "كل اللاعبين", "👥"),
        ("!resetall", "تصفير اللعبة كاملة", "⚠️"),
        ("!resetuser", "تصفير لاعب", "🔄"),
        ("!backup", "نسخة احتياطية", "💾"),
        ("!backups", "قائمة النسخ", "📋"),
        ("!logs", "سجل إجراءات المطور", "📝"),
        ("!clearlogs", "مسح السجل", "🗑️"),
        ("!broadcast", "إعلان لكل السيرفرات", "📢"),
        ("!status", "حالة البوت", "📊"),
        ("!say", "إرسال رسالة", "💬"),
        ("!welcometest", "معاينة ترحيب تجريبية", "🎉"),
        ("!restart", "إعادة تشغيل البوت", "🔄"),
        ("!shutdown", "إيقاف البوت", "🛑"),
    ],
    "ai": [
        ("@البوت سؤال", "اسأل البوت مباشرة في الشات", "💬"),
        ("DM للبوت", "أرسل سؤالك في الخاص", "📩"),
        ("رابط عام", "منشن البوت مع رابط لقراءته", "🔗"),
        ("صورة مرفقة", "أرفق حتى 4 صور للتحليل", "🖼️"),
        ("ذاكرة", "آخر 10 رسائل لكل مستخدم", "🧠"),
        ("!play", "طلب أغنية", "🎵"),
    ],
    "economy": [
        ("!الالعاب", "دليل الاقتصاد والمباني", "📖"),
        ("!بنك", "رصيدك وأراضيك والدخل", "🏦"),
        ("!شراء", "شراء أرض بـ 1,000 عملة", "🏞️"),
        ("!بناء", "بناء مبنى على أرض", "🏗️"),
        ("!جمع", "جمع دخل المباني المتراكم", "💰"),
        ("!تطوير", "تطوير مبنى حتى المستوى 5", "📈"),
        ("!اغنى", "ترتيب أغنى اللاعبين", "🏆"),
    ],
    "casino": [
        ("!كازينو", "دليل الألعاب والقواعد", "🎰"),
        ("!ماين", "لعبة النرد", "🎲"),
        ("!كنز", "لعبة الكنز", "💎"),
        ("!ابراج", "لعبة الأبراج", "🏗️"),
        ("!صاروخ", "لعبة الصاروخ", "🚀"),
        ("!استثمار", "لعبة الاستثمار", "📈"),
        ("!تداول", "لعبة التداول", "📊"),
        ("!سلوتس", "ماكينة السلوتس", "🎰"),
        ("!عجلة", "عجلة الحظ", "🎡"),
        ("!روليت", "روليت الألوان والأرقام", "🎰"),
        ("!بلاكجاك", "لعبة الورق", "🃏"),
        ("!سباق", "سباق الخيل", "🐎"),
        ("!رماية", "لعبة الرماية", "🎯"),
        ("!يانصيب", "شراء تذاكر يانصيب", "🎟️"),
        ("!تحدي", "تحدٍّ بين لاعبين", "⚔️"),
        ("!راتب", "راتب يومي مجاني", "💼"),
        ("!بخشيش", "بخشيش عشوائي", "🎁"),
    ],
    "social": [
        ("!marry", "💍 طلب زواج من عضو", "💍"),
        ("!accept", "✅ قبول طلب الزواج", "✅"),
        ("!reject", "❌ رفض طلب الزواج", "❌"),
        ("!divorce", "💔 طلاق من الزوج/الزوجة", "💔"),
        ("!mypartner", "💑 عرض زوجك/زوجتك", "💑"),
        ("!marriages", "📋 قائمة المتزوجين في السيرفر", "💕"),
        ("!friend", "👥 إضافة صديق", "🤝"),
        ("!unfriend", "❌ إزالة صديق", "👋"),
        ("!friends", "📋 قائمة أصدقائك", "📋"),
        ("!bestie", "⭐ تعيين أعز صديق", "⭐"),
    ],
    "shop": [
        ("!shop", "🛒 عرض كل عناصر المتجر", "🛒"),
        ("!purchase <id>", "💰 شراء عنصر من المتجر", "💰"),
        ("!inventory", "🎒 عرض مخزونك", "🎒"),
        ("!openbox lucky", "🎁 فتح صندوق حظ", "🎁"),
        ("!openbox mega", "💝 فتح صندوق ضخم", "💝"),
    ],
    "utility": [
        ("!weather <مدينة>", "🌤️ عرض حالة الطقس الحالية", "🌤️"),
        ("!forecast <مدينة>", "📅 توقعات 3 أيام", "📅"),
    ],
}


# ═══════════════════════════════════════
# 🎨 بناء Embed الفئة
# ═══════════════════════════════════════
def category_embed(category):
    """يبني embed أنيق للفئة."""
    meta = CATEGORY_META[category]
    entries = COMMANDS[category]
    
    embed = discord.Embed(
        title=f"{meta['emoji']}  {meta['title']}",
        description=f"> *{meta['description']}*\n\u200b",
        color=meta["color"],
    )
    
    # ═══ الأوامر ═══
    text = ""
    for name, description, emoji in entries[:20]:
        text += f"{emoji} **`{name}`**\n┗ *{description}*\n\n"
    
    if text:
        embed.add_field(
            name=f"📋 الأوامر ({len(entries)})",
            value=text[:1024],
            inline=False,
        )
    
    # ═══ الأوامر الإضافية (لو أكثر من 20) ═══
    if len(entries) > 20:
        extra = "\n".join(
            f"{emoji} **`{name}`** — *{description}*"
            for name, description, emoji in entries[20:]
        )
        embed.add_field(
            name="📌 أوامر إضافية",
            value=extra[:1024],
            inline=False,
        )
    
    # ═══ Footer ═══
    embed.set_footer(
        text=f"💡 الأوامر تبدأ بـ ! أو /  •  الفئة {meta['emoji']}",
    )
    embed.set_thumbnail(url=meta["thumbnail"])
    embed.timestamp = discord.utils.utcnow()
    
    return embed


# ═══════════════════════════════════════
# 🎯 قائمة الاختيار
# ═══════════════════════════════════════
class CommandCategorySelect(discord.ui.Select):
    def __init__(self, developer_id):
        self.developer_id = developer_id
        options = []
        
        category_emojis = {
            "general": "👤", "emotions": "💕", "admin": "🛡️",
            "dev": "👑", "ai": "🤖", "economy": "💰",
            "casino": "🎰", "social": "💍", "shop": "🛒",
            "utility": "🌤️",
        }
        
        for key, meta in CATEGORY_META.items():
            options.append(
                discord.SelectOption(
                    label=meta["title"],
                    value=key,
                    description=meta["description"][:100],
                    emoji=category_emojis.get(key, "📁"),
                )
            )
        
        super().__init__(
            placeholder="🎯 اختر فئة الأوامر من القائمة...",
            min_values=1,
            max_values=1,
            options=options,
        )
    
    async def callback(self, interaction):
        category = self.values[0]
        if category == "dev" and interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "🔒 **هذه الأوامر خاصة بمطور البوت فقط.**",
                ephemeral=True,
            )
            return
        
        await interaction.response.send_message(
            embed=category_embed(category),
            ephemeral=True,
        )


class CommandCategoryView(discord.ui.View):
    def __init__(self, developer_id):
        super().__init__(timeout=300)
        self.message = None
        self.add_item(CommandCategorySelect(developer_id))
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
            item.placeholder = "⏰ انتهت المهلة؛ اكتب !command لفتح قائمة جديدة"
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.warning("Could not disable expired command menu.")


# ═══════════════════════════════════════
# 📤 إرسال القائمة الرئيسية
# ═══════════════════════════════════════
async def send_command_menu(ctx, developer_id):
    """يرسل القائمة الرئيسية."""
    
    # احسب عدد الأوامر
    total = sum(len(cmds) for cmds in COMMANDS.values())
    
    embed = discord.Embed(
        title="📚  قائمة أوامر البوت",
        description=(
            f"أهلاً بك! 👋\n\n"
            f"اختر فئة من القائمة المنسدلة أدناه لاستعراض الأوامر.\n"
            f"\u200b"
        ),
        color=0x5865F2,
    )
    
    # ═══ الفئات ═══
    embed.add_field(
        name="👤 الأوامر العامة",
        value="`متاحة للجميع`",
        inline=True,
    )
    embed.add_field(
        name="💕 المشاعر والعلاقة",
        value="`ملف شخصي`",
        inline=True,
    )
    embed.add_field(
        name="🛡️ أوامر الإدارة",
        value="`للمشرفين`",
        inline=True,
    )
    embed.add_field(
        name="👑 أوامر المطور",
        value="`للمطور فقط`",
        inline=True,
    )
    embed.add_field(
        name="🤖 الذكاء الاصطناعي",
        value="`محادثة ذكية`",
        inline=True,
    )
    embed.add_field(
        name="💰 الأراضي والاقتصاد",
        value="`بناء واستثمار`",
        inline=True,
    )
    embed.add_field(
        name="🎰 الألعاب والمكافآت",
        value="`17 لعبة`",
        inline=True,
    )
    embed.add_field(
        name="💍 الزواج والأصدقاء",
        value="`تفاعل اجتماعي`",
        inline=True,
    )
    embed.add_field(
        name="🛒 المتجر",
        value="`شراء عناصر`",
        inline=True,
    )
    embed.add_field(
        name="🌤️ أدوات مفيدة",
        value="`طقس وتوقعات`",
        inline=True,
    )
    
    # ═══ Footer ═══
    embed.add_field(
        name="\u200b",
        value=(
            f"📊 **الإجمالي:** `{total}` أمر متاح\n"
            f"⏰ **مدة القائمة:** 5 دقائق\n"
            f"🔒 **الخصوصية:** ردود الفئات تظهر لك وحدك"
        ),
        inline=False,
    )
    
    embed.set_footer(
        text="💡 اختر فئة من القائمة المنسدلة لاستعراض الأوامر",
    )
    if ctx.bot.user:
        embed.set_thumbnail(url=ctx.bot.user.display_avatar.url)
    
    view = CommandCategoryView(developer_id)
    view.message = await ctx.reply(
        embed=embed,
        view=view,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )