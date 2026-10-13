"""Discord command adapters for developer-only game administration."""

from __future__ import annotations

import re
import inspect
from datetime import datetime

import discord
from discord.ext import commands

from admin_engine import AdminEngine, AdminError, MAX_ADMIN_AMOUNT, MAX_BROADCAST_LENGTH
from economy_commands import BUILDINGS


def _target_id(token, ctx):
    """Parse a mention, raw ID, or the documented ``me`` shortcut."""
    if token is None:
        raise AdminError("Usage: provide a user mention or ID.")
    if str(token).lower() == "me":
        return str(ctx.author.id)
    mention = re.fullmatch(r"<@!?(\d+)>", str(token))
    raw = mention.group(1) if mention else str(token)
    if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise AdminError("Use a user mention or numeric user ID.")
    return raw


async def _resolve_user(bot, ctx, token):
    uid = _target_id(token, ctx)
    mention = next((user for user in getattr(ctx.message, "mentions", ()) if str(user.id) == uid), None)
    if mention is not None:
        return mention
    if int(uid) == ctx.author.id:
        return ctx.author
    try:
        return await bot.fetch_user(int(uid))
    except discord.NotFound:
        raise AdminError("That Discord user could not be found.") from None
    except (discord.Forbidden, discord.HTTPException):
        raise AdminError("Could not resolve that user right now.") from None


class AdminConfirmationView(discord.ui.View):
    """One-shot confirmation whose buttons only the developer can trigger."""

    def __init__(self, developer_id, operation, *, timeout=45):
        super().__init__(timeout=timeout)
        self.developer_id = developer_id
        self.operation = operation
        self.finished = False
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "التأكيد متاح للمطور فقط.", ephemeral=True
            )
            return False
        return True

    def _finish(self):
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="تأكيد", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self.finished:
            await interaction.response.send_message("انتهت صلاحية هذا التأكيد.", ephemeral=True)
            return
        self._finish()
        try:
            result = self.operation()
            if inspect.isawaitable(result):
                result = await result
            text = f"✅ تم التنفيذ. {result}".strip()
        except AdminError as exc:
            text = f"❌ {exc}"
        except Exception:
            # Do not expose filesystem paths, serialized state, or exception data.
            text = "❌ تعذر تنفيذ العملية. راجع سجل البوت لمعرفة السبب."
        await interaction.response.edit_message(content=text, embed=None, view=self)

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.finished:
            await interaction.response.send_message("انتهت صلاحية هذا التأكيد.", ephemeral=True)
            return
        self._finish()
        await interaction.response.edit_message(content="تم الإلغاء؛ لم تُنفذ العملية.", embed=None, view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(content="انتهت مهلة التأكيد؛ لم تُنفذ العملية.", embed=None, view=self)
            except discord.HTTPException:
                pass


def setup_admin_commands(bot, store, developer_id):
    """Synchronously register all developer commands exactly once."""
    if getattr(bot, "_admin_game", None) is not None:
        return bot._admin_game
    engine = AdminEngine(store)

    async def is_developer(ctx):
        return ctx.author.id == developer_id

    def developer_only(function):
        return commands.check(is_developer)(function)

    async def run_admin(ctx, operation):
        try:
            return operation()
        except AdminError as exc:
            await ctx.reply(f"❌ {exc}")
            return None

    async def ask_confirmation(ctx, title, description, operation):
        view = AdminConfirmationView(developer_id, operation)
        embed = discord.Embed(title=title, description=description, color=0xE74C3C)
        view.message = await ctx.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    @bot.command(name="resetall", aliases=["reset_game", "تصفير"])
    @developer_only
    async def reset_all(ctx):
        count = len(engine.snapshot().get("users", {}))
        await ask_confirmation(
            ctx, "هل تريد تصفير اللعبة بالكامل؟",
            f"سيُحذف تقدم {count} لاعب وتذاكر اليانصيب وفترات الانتظار والحظر. "
            "ستُنشأ نسخة احتياطية محلية أولاً؛ سيبقى سجل الإجراءات.",
            lambda: _result_text("عدد اللاعبين المحذوفين", engine.reset_all()),
        )

    @bot.command(name="resetuser", aliases=["reset_player", "تصفير_لاعب"])
    @developer_only
    async def reset_user(ctx, target: str = None):
        if target is None:
            await ctx.reply("Usage: `!resetuser @user`")
            return
        user = await _resolve_user(bot, ctx, target)
        await ask_confirmation(
            ctx, "هل تريد تصفير هذا اللاعب؟",
            f"ستُحذف بيانات {discord.utils.escape_markdown(user.name)} وتذاكره في اليانصيب "
            "ومهلاته وحظره. ستُنشأ نسخة احتياطية محلية أولاً.",
            lambda: _result_text("تم تصفير اللاعب", engine.reset_user(user.id)),
        )

    @bot.command(name="addmoney", aliases=["add_money"])
    @developer_only
    async def add_money(ctx, target: str = None, amount: str = None):
        if target is None or amount is None:
            await ctx.reply("Usage: `!addmoney @user <amount>`")
            return
        user = await _resolve_user(bot, ctx, target)
        value = await run_admin(ctx, lambda: engine.adjust_money(user.id, amount, "add"))
        if value is not None:
            await ctx.reply(f"✅ Added virtual credits to {user.mention}. Balance: `{value:,}`.")

    @bot.command(name="removemoney", aliases=["remove_money"])
    @developer_only
    async def remove_money(ctx, target: str = None, amount: str = None):
        if target is None or amount is None:
            await ctx.reply("Usage: `!removemoney @user <amount>`")
            return
        user = await _resolve_user(bot, ctx, target)
        value = await run_admin(ctx, lambda: engine.adjust_money(user.id, amount, "remove"))
        if value is not None:
            await ctx.reply(f"✅ Removed virtual credits from {user.mention}. Balance: `{value:,}`.")

    @bot.command(name="setmoney", aliases=["set_money"])
    @developer_only
    async def set_money(ctx, target: str = None, amount: str = None):
        if target is None or amount is None:
            await ctx.reply("Usage: `!setmoney @user <amount>`")
            return
        user = await _resolve_user(bot, ctx, target)
        value = await run_admin(ctx, lambda: engine.adjust_money(user.id, amount, "set"))
        if value is not None:
            await ctx.reply(f"✅ Set {user.mention}'s virtual balance to `{value:,}`.")

    @bot.command(name="givemoney", aliases=["give_money"])
    @developer_only
    async def give_money(ctx, amount: str = None):
        if amount is None:
            await ctx.reply("Usage: `!givemoney <amount>`")
            return
        try:
            amount_value = int(amount)
            if not str(amount).isascii() or not str(amount).isdecimal() or amount_value > MAX_ADMIN_AMOUNT:
                raise ValueError
        except (ValueError, TypeError):
            await ctx.reply(f"❌ Amount must be a whole number from 0 to {MAX_ADMIN_AMOUNT:,}.")
            return
        count = len(engine.snapshot().get("users", {}))
        await ask_confirmation(
            ctx, "منح رصيد لجميع اللاعبين؟",
            f"ستُضاف `{amount_value:,}` عملة افتراضية إلى {count} لاعب محفوظ. ستُنشأ نسخة احتياطية محلية أولاً.",
            lambda: _result_text(f"تم المنح لـ {engine.give_money(amount_value)} لاعب", None),
        )

    @bot.command(name="addlands", aliases=["add_land", "أراضي"])
    @developer_only
    async def add_lands(ctx, target: str = None, count: str = "1"):
        if target is None:
            await ctx.reply("Usage: `!addlands @user <1-100>`")
            return
        user = await _resolve_user(bot, ctx, target)
        total = await run_admin(ctx, lambda: engine.add_lands(user.id, count))
        if total is not None:
            await ctx.reply(f"✅ Added lands to {user.mention}. Total: `{total}`.")

    @bot.command(name="addincome", aliases=["add_income", "دخل"])
    @developer_only
    async def add_income(ctx, target: str = None, amount: str = None):
        if target is None or amount is None:
            await ctx.reply("Usage: `!addincome @user <credits/hour>`")
            return
        user = await _resolve_user(bot, ctx, target)
        try:
            from admin_engine import nonnegative_amount
            hourly = nonnegative_amount(amount)
        except AdminError as exc:
            await ctx.reply(f"❌ {exc}")
            return
        result = await run_admin(ctx, lambda: engine.set_bonus_income(user.id, hourly) or True)
        if result is not None:
            await ctx.reply(f"✅ تم تحديد الدخل الإضافي لـ {user.mention} بـ `{hourly:,}` عملة افتراضية/ساعة.")

    @bot.command(name="block", aliases=["ban_game", "حظر"])
    @developer_only
    async def block(ctx, target: str = None):
        if target is None:
            await ctx.reply("Usage: `!block @user`")
            return
        user = await _resolve_user(bot, ctx, target)
        if await run_admin(ctx, lambda: _engine_result(engine.block_user(user.id), "blocked")) is not None:
            await ctx.reply(f"🚫 {user.mention} is blocked from game commands.")

    @bot.command(name="unblock", aliases=["unban_game", "الغاء_حظر"])
    @developer_only
    async def unblock(ctx, target: str = None):
        if target is None:
            await ctx.reply("Usage: `!unblock @user`")
            return
        user = await _resolve_user(bot, ctx, target)
        if await run_admin(ctx, lambda: _engine_result(engine.unblock_user(user.id), "unblocked")) is not None:
            await ctx.reply(f"✅ {user.mention} is unblocked.")

    @bot.command(name="blocked", aliases=["blocked_list", "المحظورين"])
    @developer_only
    async def blocked(ctx):
        state = engine.snapshot()
        entries = state.get("blocked_users", [])
        ids = sorted(str(value) for value in (entries.keys() if isinstance(entries, dict) else entries))
        await ctx.reply("No blocked players." if not ids else "🚫 Blocked IDs: " + ", ".join(f"`{uid}`" for uid in ids[:50]))

    @bot.command(name="logs", aliases=["devlogs", "سجل"])
    @developer_only
    async def logs(ctx, count: str = "10"):
        try:
            limit = int(count)
            if not str(count).isascii() or not str(count).isdecimal() or not 1 <= limit <= 100:
                raise ValueError
        except (TypeError, ValueError):
            await ctx.reply("Count must be an integer from 1 to 100.")
            return
        entries = engine.snapshot().get("admin_logs", [])[-limit:][::-1]
        if not entries:
            await ctx.reply("No admin operations are recorded.")
            return
        for offset in range(0, len(entries), 10):
            embed = discord.Embed(
                title=f"سجل إجراءات المطور ({offset + 1}-{min(offset + 10, len(entries))})",
                color=0xF1C40F,
            )
            for entry in entries[offset:offset + 10]:
                embed.add_field(
                    name=str(entry.get("action", "operation"))[:200],
                    value=f"{str(entry.get('details', ''))[:350]}\n{str(entry.get('time', ''))[:100]}",
                    inline=False,
                )
            await ctx.send(embed=embed)

    @bot.command(name="clearlogs", aliases=["clear_logs", "مسح_السجل"])
    @developer_only
    async def clear_logs(ctx):
        count = len(engine.snapshot().get("admin_logs", []))
        await ask_confirmation(
            ctx, "مسح سجل المطور؟",
            f"سيُحذف {count} سجل قديم. ستُنشأ نسخة احتياطية محلية أولاً، وسيُسجل هذا الإجراء.",
            lambda: _result_text("تم مسح السجل القديم", engine.clear_logs()),
        )

    @bot.command(name="stats", aliases=["game_stats", "احصائيات"])
    @developer_only
    async def stats(ctx):
        state = engine.snapshot()
        users = state.get("users", {})
        total_money = sum(int(user.get("money", 0)) for user in users.values())
        total_earned = sum(int(user.get("total_earned", 0)) for user in users.values())
        total_lands = sum(len(user.get("lands", [])) for user in users.values())
        buildings = {}
        for user in users.values():
            for land in user.get("lands", []):
                key = land.get("building")
                if key:
                    buildings[key] = buildings.get(key, 0) + 1
        embed = discord.Embed(title="Game statistics", color=0xF1C40F, timestamp=datetime.now())
        embed.add_field(name="Players", value=str(len(users)))
        embed.add_field(name="Total balance", value=f"{total_money:,}")
        embed.add_field(name="Total earned", value=f"{total_earned:,}")
        embed.add_field(name="Lands", value=str(total_lands))
        embed.add_field(name="Blocked", value=str(len(state.get("blocked_users", []))))
        if buildings:
            embed.add_field(name="Buildings", value="\n".join(
                f"{BUILDINGS.get(key, {}).get('name', key)}: {number}"
                for key, number in sorted(buildings.items())
            ), inline=False)
        await ctx.send(embed=embed)

    @bot.command(name="moneyboard", aliases=["money_board", "الاقتصاد"])
    @developer_only
    async def moneyboard(ctx):
        users = engine.snapshot().get("users", {})
        richest = sorted(users.items(), key=lambda item: item[1].get("money", 0), reverse=True)[:10]
        if not richest:
            await ctx.reply("No saved players.")
            return
        embed = discord.Embed(title="Virtual economy leaderboard", color=0xF1C40F)
        for rank, (uid, player) in enumerate(richest, 1):
            embed.add_field(name=f"{rank}. Player {uid}", value=f"{int(player.get('money', 0)):,} credits")
        await ctx.send(embed=embed)

    @bot.command(name="backup", aliases=["نسخ_احتياطي"])
    @developer_only
    async def backup(ctx):
        try:
            path = engine.record_backup()
        except AdminError as exc:
            await ctx.reply(f"❌ {exc}")
            return
        await ctx.reply(f"✅ Private local backup created: `{path.name}`")

    @bot.command(name="backups", aliases=["backup_list", "النسخ"])
    @developer_only
    async def backups(ctx):
        files = sorted(engine.backup_dir.glob("backup_*.json"), key=lambda item: item.name, reverse=True)
        if not files:
            await ctx.reply("No local backups are available.")
            return
        embed = discord.Embed(title="Private local backups", color=0x3498DB)
        for path in files[:10]:
            embed.add_field(name=path.name, value=f"{path.stat().st_size:,} bytes", inline=False)
        await ctx.send(embed=embed)

    @bot.command(name="allplayers", aliases=["players_list", "اللاعبين"])
    @developer_only
    async def all_players(ctx):
        users = engine.snapshot().get("users", {})
        players = sorted(users.items(), key=lambda item: item[1].get("money", 0), reverse=True)
        if not players:
            await ctx.reply("No saved players.")
            return
        rows = [f"`{index}.` ID `{uid}` — `{int(user.get('money', 0)):,}` credits — "
                f"`{len(user.get('lands', []))}` lands"
                for index, (uid, user) in enumerate(players[:20], 1)]
        if len(players) > 20:
            rows.append(f"... and {len(players) - 20} more players")
        await ctx.send(embed=discord.Embed(
            title=f"Players ({len(players)})", description="\n".join(rows), color=0x3498DB
        ))

    @bot.command(name="broadcast", aliases=["اعلان_عام"])
    @developer_only
    async def broadcast(ctx, *, message: str = None):
        if not message or len(message) > MAX_BROADCAST_LENGTH:
            await ctx.reply(f"Usage: `!broadcast <message>` (maximum {MAX_BROADCAST_LENGTH} characters)")
            return
        guilds = list(bot.guilds)

        async def send_broadcast():
            sent = failed = 0
            embed = discord.Embed(title="Developer announcement", description=message,
                                  color=0xF1C40F, timestamp=datetime.now())
            for guild in guilds:
                me = getattr(guild, "me", None)
                candidates = []
                if guild.system_channel is not None:
                    candidates.append(guild.system_channel)
                candidates.extend(channel for channel in guild.text_channels
                                  if channel not in candidates)
                target = None
                for channel in candidates:
                    try:
                        perms = channel.permissions_for(me) if me is not None else None
                    except (AttributeError, discord.DiscordException):
                        perms = None
                    if perms and perms.send_messages and perms.embed_links:
                        target = channel
                        break
                if target is None:
                    failed += 1
                    continue
                try:
                    await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    sent += 1
                except discord.HTTPException:
                    failed += 1
            engine.record_broadcast(sent, failed)
            return f"sent to {sent} guilds; {failed} failed or had no writable channel"

        await ask_confirmation(
            ctx, f"Broadcast to {len(guilds)} servers?",
            f"Send this announcement once per eligible server with mentions disabled:\n\n{message}",
            send_broadcast,
        )

    @bot.command(name="adminmenu", aliases=["admin_help", "اوامر_المطور"])
    @developer_only
    async def admin_menu(ctx):
        embed = discord.Embed(title="أوامر المطور", color=0xF1C40F)
        embed.add_field(name="الرصيد الافتراضي",
                        value="`!addmoney @user <n>` · `!removemoney @user <n>` · "
                              "`!setmoney @user <n>` · `!givemoney <n>`", inline=False)
        embed.add_field(name="الأراضي والدخل",
                        value="`!addlands @user <1-100>` · `!addincome @user <per-hour>`", inline=False)
        embed.add_field(name="الحظر والإحصائيات",
                        value="`!block @user` · `!unblock @user` · `!blocked` · `!stats` · "
                              "`!moneyboard` · `!allplayers`", inline=False)
        embed.add_field(name="إدارة بيانات اللعبة",
                        value="`!resetall` · `!resetuser @user` · `!backup` · `!backups`", inline=False)
        embed.add_field(name="السجل والإعلان",
                        value="`!logs [1-100]` · `!clearlogs` · `!broadcast <message>`", inline=False)
        embed.add_field(name="التحكم بالبوت",
                        value="`!status` · `!say <رسالة>` · `!welcometest` · `!resetcool` · "
                              "`!restart` · `!shutdown`", inline=False)
        await ctx.send(embed=embed)

    command_help = {
        "resetall": "!resetall — reset game state (developer confirmation required).",
        "resetuser": "!resetuser @user — reset one player (developer confirmation required).",
        "addmoney": "!addmoney @user <amount> — add virtual credits.",
        "removemoney": "!removemoney @user <amount> — remove virtual credits.",
        "setmoney": "!setmoney @user <amount> — set virtual balance.",
        "givemoney": "!givemoney <amount> — grant credits to all players after confirmation.",
        "addlands": "!addlands @user <1-100> — add lands.",
        "addincome": "!addincome @user <amount/hour> — set bonus hourly income.",
        "block": "!block @user — block game access.",
        "unblock": "!unblock @user — restore game access.",
        "blocked": "!blocked — list blocked user IDs.",
        "logs": "!logs [1-100] — display recent admin audit entries.",
        "clearlogs": "!clearlogs — clear old audit entries after confirmation.",
        "stats": "!stats — game and economy statistics.",
        "moneyboard": "!moneyboard — richest saved players.",
        "backup": "!backup — create a private local full-state backup.",
        "backups": "!backups — list local backup files.",
        "allplayers": "!allplayers — list saved players.",
        "broadcast": "!broadcast <message> — announce after confirmation.",
        "adminmenu": "!adminmenu — show developer commands.",
    }
    for name, help_text in command_help.items():
        command = bot.get_command(name)
        command.help = help_text
        command.extras["admin_game"] = True

    bot._admin_game = engine
    return engine


def _engine_result(value, label):
    return f"{label}: {value}" if label else "updated"


def _result_text(label, result):
    return label if result is None else f"{label}: {result}"