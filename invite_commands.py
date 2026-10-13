"""Commands for creating, inspecting, and removing Discord server invites."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path

import discord


logger = logging.getLogger(__name__)

MAX_PERMANENT_INVITES_PER_BATCH = 10
BOT_INVITE_CONFIRMATION_TIMEOUT_SECONDS = 120
INVITE_RETRY_COOLDOWN_SECONDS = 60


class InviteReportRecoveryStore:
    """Persist failed private invite reports with atomic, private file writes."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path is not None else None
        self.reports = self._load()

    def _load(self) -> list[list[str]]:
        if self.path is None:
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Could not load the pending invite report recovery file."
            ) from error

        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "reports"}
            or type(payload.get("version")) is not int
            or payload["version"] != 1
            or not isinstance(payload.get("reports"), list)
        ):
            raise ValueError("Invalid pending invite report recovery data.")

        reports = []
        for report in payload["reports"]:
            if (
                not isinstance(report, list)
                or not report
                or any(not isinstance(chunk, str) or not chunk for chunk in report)
            ):
                raise ValueError("Invalid pending invite report recovery data.")
            reports.append(list(report))
        return reports

    def _save(self, reports: list[list[str]]) -> None:
        if self.path is None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {"version": 1, "reports": reports},
                    output,
                    ensure_ascii=False,
                    indent=2,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def snapshot(self) -> list[list[str]]:
        return [list(report) for report in self.reports]

    def add_report(self, report_chunks: list[str]) -> bool:
        if not report_chunks or any(
            not isinstance(chunk, str) or not chunk for chunk in report_chunks
        ):
            raise ValueError("A pending invite report must contain message chunks.")
        updated = [*self.reports, list(report_chunks)]
        try:
            self._save(updated)
        except OSError:
            logger.exception("Could not persist a pending developer invite report.")
            return False
        self.reports = updated
        return True

    def remove_report(self, report_chunks: list[str]) -> bool:
        for index, report in enumerate(self.reports):
            if report == report_chunks:
                updated = [*self.reports[:index], *self.reports[index + 1 :]]
                self._save(updated)
                self.reports = updated
                return True
        return False

    def discard_all(self) -> int:
        """Remove every pending report only after the empty queue is saved."""
        discarded_count = len(self.reports)
        if not discarded_count:
            return 0
        self._save([])
        self.reports = []
        return discarded_count


class NoInviteChannelError(Exception):
    """Raised when the bot cannot create an invite in any text channel."""


class InviteInspectionError(Exception):
    """Raised when a report cannot inspect a guild's existing invites."""


def _invite_inspection_failure_reason(error: InviteInspectionError) -> str:
    """Describe whether invite inspection failed due to permissions or Discord."""
    cause = error.__cause__
    if isinstance(cause, discord.Forbidden):
        return "تعذر فحص الدعوات: يفتقد البوت صلاحية Manage Server."
    if isinstance(cause, discord.HTTPException):
        return "تعذر فحص الدعوات بسبب خطأ مؤقت من Discord؛ أعد المحاولة لاحقًا."
    return "تعذر فحص الدعوات لسبب غير معروف."


def _is_temporary_invite_inspection_error(error: InviteInspectionError) -> bool:
    """Return whether an inspection failure is retryable rather than permission-based."""
    cause = error.__cause__
    return isinstance(cause, discord.HTTPException) and not isinstance(
        cause, discord.Forbidden
    )


async def _find_permanent_invite(guild, *, require_inspection: bool = False):
    """Return an existing unlimited invite when the bot can inspect guild invites."""
    try:
        invites = await guild.invites()
    except discord.Forbidden as error:
        # Listing invites requires Manage Server; invite creation may still be allowed.
        logger.debug("Cannot list invites in guild %s; will try creating one.", guild.id)
        if require_inspection:
            raise InviteInspectionError from error
        return None
    except discord.HTTPException as error:
        logger.exception("Could not list invites in guild %s.", guild.id)
        if require_inspection:
            raise InviteInspectionError from error
        return None

    for invite in invites:
        if (
            getattr(invite, "max_age", None) == 0
            and getattr(invite, "max_uses", None) == 0
            and not getattr(invite, "revoked", False)
        ):
            return invite
    return None


def _find_invite_channel(guild, preferred_channel=None):
    """Find a text channel where the bot can create an invite."""
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        return None

    candidates = [
        preferred_channel,
        getattr(preferred_channel, "parent", None),
        *getattr(guild, "text_channels", ()),
    ]
    seen_ids: set[int] = set()
    for channel in candidates:
        if channel is None or not callable(getattr(channel, "create_invite", None)):
            continue
        channel_id = getattr(channel, "id", None)
        if channel_id is not None:
            if channel_id in seen_ids:
                continue
            seen_ids.add(channel_id)
        try:
            if channel.permissions_for(bot_member).create_instant_invite:
                return channel
        except (AttributeError, discord.HTTPException):
            continue
    return None


async def _get_or_create_permanent_invite(
    guild,
    preferred_channel,
    requester_id: int,
    *,
    require_inspection: bool = False,
):
    existing = await _find_permanent_invite(
        guild,
        require_inspection=require_inspection,
    )
    if existing is not None:
        return existing

    channel = _find_invite_channel(guild, preferred_channel)
    if channel is None:
        raise NoInviteChannelError

    return await channel.create_invite(
        max_age=0,
        max_uses=0,
        unique=False,
        temporary=False,
        reason=f"Permanent server invite requested by member {requester_id}",
    )


def _developer_invite_report_chunks(
    lines: list[str],
    *,
    create_missing: bool = False,
    needs_attention: bool = False,
    retry_temporary: bool = False,
    summary_counts: tuple[int, int, int, int, int, int] | None = None,
) -> list[str]:
    """Split a private guild invite report into messages below Discord's limit."""
    if retry_temporary:
        mode = "إعادة فحص أخطاء Discord المؤقتة فقط"
    elif needs_attention:
        mode = "السيرفرات التي تحتاج إلى مراجعة فقط"
    else:
        mode = "إنشاء الروابط المفقودة" if create_missing else "دون إنشاء روابط جديدة"
    header = f"قائمة السيرفرات وروابط الدعوة ({mode}):\n"
    continuation_header = f"قائمة السيرفرات وروابط الدعوة ({mode}، تابع):\n"
    chunks: list[str] = []
    current = header
    if summary_counts is not None:
        (
            reusable_count,
            missing_count,
            inspection_failure_count,
            permission_failure_count,
            temporary_failure_count,
            other_inspection_failure_count,
        ) = summary_counts
        current += (
            "ملخص السيرفرات المعروضة:\n"
            f"• دعوات دائمة قابلة لإعادة الاستخدام: **{reusable_count}**\n"
            f"• لا توجد دعوة دائمة قابلة لإعادة الاستخدام: **{missing_count}**\n"
            f"• تعذر فحص الدعوات: **{inspection_failure_count}**\n"
            f"  - نقص صلاحية Manage Server: **{permission_failure_count}**\n"
            f"  - أخطاء مؤقتة من Discord: **{temporary_failure_count}**\n"
            f"  - أسباب أخرى: **{other_inspection_failure_count}**\n"
        )

    for line in lines:
        if len(current) + len(line) + 1 > 1900 and current not in (
            header,
            continuation_header,
        ):
            chunks.append(current)
            current = continuation_header
        current += f"{line}\n"

    chunks.append(current.rstrip())
    return chunks


def _parse_developer_invite_report_options(
    options: tuple[str, ...],
) -> tuple[list[int] | None, bool, bool]:
    """Parse selected guild IDs, creation choice, and needs-attention filter."""
    guild_ids = []
    create_missing = False
    needs_attention = False
    for option in options:
        value = str(option).strip().casefold()
        if value in {"إنشاء", "انشاء", "create"} and not create_missing:
            create_missing = True
        elif value in {
            "مراجعة",
            "تحتاج_مراجعة",
            "attention",
            "needs_attention",
        } and not needs_attention:
            needs_attention = True
        elif "," in value:
            # Prefix-command parsing splits on spaces, so a user typing "10, 11"
            # may pass "10," and "11" as separate options.
            if value.endswith(","):
                value = value[:-1].strip()
            parts = [part.strip() for part in value.split(",")]
            if not parts or any(not part.isdecimal() for part in parts):
                raise ValueError(
                    "Expected comma-separated positive guild IDs."
                )
            guild_ids.extend(int(part) for part in parts)
        elif value.isdecimal():
            guild_ids.append(int(value))
        else:
            raise ValueError(
                "Expected guild IDs, مراجعة, and/or إنشاء."
            )
    if any(guild_id <= 0 for guild_id in guild_ids):
        raise ValueError("Guild IDs must be positive.")
    if len(guild_ids) != len(set(guild_ids)):
        raise ValueError("Guild IDs must be unique.")
    if create_missing and needs_attention:
        raise ValueError("مراجعة cannot be combined with إنشاء.")
    return guild_ids or None, create_missing, needs_attention


def _invite_embed(guild, invite, title: str) -> discord.Embed:
    guild_name = discord.utils.escape_markdown(
        str(getattr(guild, "name", "السيرفر")).replace("\n", " ")
    )[:200]
    member_count = getattr(guild, "member_count", None)
    members = str(member_count) if member_count is not None else "غير متاح"
    embed = discord.Embed(
        title=title.format(guild_name=guild_name),
        description=(
            "**دعوة دائمة وغير محدودة**\n"
            "لا تنتهي تلقائيًا، لكن يمكن لإدارة السيرفر إلغاؤها.\n\n"
            f"```{invite.url}```\n"
            f"الأعضاء الحاليون: **{members}**"
        ),
        color=0x00A86B,
        timestamp=discord.utils.utcnow(),
    )
    icon = getattr(guild, "icon", None)
    if icon is not None:
        embed.set_thumbnail(url=icon.url)
    embed.set_footer(text=guild_name)
    return embed


def _invites_embed(guild, invites) -> discord.Embed:
    embed = discord.Embed(
        title="إحصاءات الدعوات",
        description=f"السيرفر: **{discord.utils.escape_markdown(guild.name)}**",
        color=0x4A90E2,
    )
    total_uses = sum(getattr(invite, "uses", 0) or 0 for invite in invites)
    embed.add_field(name="عدد الدعوات النشطة", value=str(len(invites)), inline=True)
    embed.add_field(name="إجمالي الاستخدامات", value=str(total_uses), inline=True)

    uses_by_inviter: dict[int, int] = {}
    for invite in invites:
        inviter = getattr(invite, "inviter", None)
        inviter_id = getattr(inviter, "id", None)
        if inviter_id is not None:
            uses_by_inviter[inviter_id] = (
                uses_by_inviter.get(inviter_id, 0)
                + (getattr(invite, "uses", 0) or 0)
            )

    top_inviters = sorted(
        uses_by_inviter.items(),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    top_text = (
        "\n".join(f"<@{user_id}> — **{uses}** استخدام" for user_id, uses in top_inviters)
        or "لا توجد بيانات استخدام متاحة."
    )
    embed.add_field(name="أكثر الداعين استخدامًا", value=top_text, inline=False)
    embed.set_footer(text="استخدام الدعوات يُحدّثه Discord وقد لا يظهر فورًا.")
    return embed


def setup_invite_commands(
    bot,
    developer_id: int,
    *,
    pending_reports_path: str | Path | None = None,
):
    """Register invite commands once and return the registered command mapping."""
    existing = getattr(bot, "_invite_commands", None)
    if existing is not None:
        return existing

    pending_reports = InviteReportRecoveryStore(pending_reports_path)
    pending_reports_lock = asyncio.Lock()
    retryable_inspection_guilds: dict[int, object] = {}
    retry_cooldown_until: dict[int, float] = {}
    retry_guild_locks: dict[int, asyncio.Lock] = {}

    def replace_retryable_inspection_guilds(retryable: dict[int, object]) -> None:
        retryable_inspection_guilds.clear()
        retryable_inspection_guilds.update(retryable)
        for guild_id in tuple(retry_cooldown_until):
            if guild_id not in retryable:
                retry_cooldown_until.pop(guild_id, None)

    async def reply(ctx, message: str):
        await ctx.reply(
            message,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def authorize_invite_interaction(interaction: discord.Interaction) -> bool:
        if getattr(getattr(interaction, "user", None), "id", None) == developer_id:
            return True
        await interaction.response.send_message(
            "هذا الإجراء خاص بمطور البوت فقط.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    class DeveloperInviteReportPagesView(discord.ui.View):
        def __init__(self, report_chunks: list[str]):
            super().__init__(timeout=900)
            self.report_chunks = report_chunks
            self.page = 0
            self.expires_at = time.monotonic() + 900
            self.expired = False
            self.message = None
            self._add_page_controls()

        async def on_timeout(self):
            if self.expired:
                return

            self.expired = True
            self.clear_items()
            if self.message is None:
                return

            try:
                await self.message.edit(
                    content=(
                        "انتهت صلاحية التقرير ولم يعد نشطًا. "
                        "أرسل الأمر مجددًا لإنشاء تقرير جديد."
                    ),
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not mark developer invite report expired.")

        async def _reject_expired_interaction(
            self,
            interaction: discord.Interaction,
        ) -> bool:
            if not self.expired and time.monotonic() < self.expires_at:
                return False

            if not self.expired:
                await self.on_timeout()

            await interaction.response.send_message(
                "انتهت صلاحية التقرير ولم يعد نشطًا. "
                "أرسل الأمر مجددًا لإنشاء تقرير جديد.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        def _add_page_controls(self):
            self.clear_items()
            previous_button = discord.ui.Button(
                label="السابق",
                style=discord.ButtonStyle.secondary,
                disabled=self.page <= 0,
            )
            next_button = discord.ui.Button(
                label="التالي",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= len(self.report_chunks) - 1,
            )

            async def previous_callback(interaction: discord.Interaction):
                if not await authorize_invite_interaction(interaction):
                    return
                if await self._reject_expired_interaction(interaction):
                    return
                self.page -= 1
                self._add_page_controls()
                await interaction.response.edit_message(
                    content=self.report_chunks[self.page],
                    view=self,
                )

            async def next_callback(interaction: discord.Interaction):
                if not await authorize_invite_interaction(interaction):
                    return
                if await self._reject_expired_interaction(interaction):
                    return
                self.page += 1
                self._add_page_controls()
                await interaction.response.edit_message(
                    content=self.report_chunks[self.page],
                    view=self,
                )

            previous_button.callback = previous_callback
            next_button.callback = next_callback
            self.add_item(previous_button)
            self.add_item(next_button)

    def render_bot_invite_page(entries, page: int, scan_errors: list[str]) -> str:
        page_size = 8
        page_count = max(1, (len(entries) + page_size - 1) // page_size)
        start = page * page_size
        page_entries = entries[start : start + page_size]
        lines = [
            f"دعوات أنشأها البوت — الصفحة {page + 1}/{page_count}:",
        ]
        for guild, invite in page_entries:
            guild_name = discord.utils.escape_markdown(
                str(getattr(guild, "name", "سيرفر")).replace("\n", " ")
            )[:80]
            url = getattr(invite, "url", None) or (
                f"https://discord.gg/{getattr(invite, 'code', '')}"
            )
            lines.append(f"• {guild_name} | `{guild.id}` | {url}")
        if scan_errors:
            lines.extend(["", "تعذر فحص بعض السيرفرات:"])
            lines.extend(f"• {name}" for name in scan_errors[:5])
            if len(scan_errors) > 5:
                lines.append(f"• وسيرفرات أخرى: {len(scan_errors) - 5}")
        lines.extend(
            [
                "",
                "اختر دعوة من القائمة لإلغائها. ستحتاج إلى تأكيد الإلغاء قبل الحذف.",
            ]
        )
        return "\n".join(lines)

    class BotInviteConfirmationView(discord.ui.View):
        def __init__(self, guild_id: int, invite_code: str, guild_name: str):
            super().__init__(timeout=BOT_INVITE_CONFIRMATION_TIMEOUT_SECONDS)
            self.guild_id = guild_id
            self.invite_code = invite_code
            self.guild_name = guild_name
            self.expires_at = time.monotonic() + BOT_INVITE_CONFIRMATION_TIMEOUT_SECONDS
            self.confirmation_message = None
            self.completed = False
            self.expired = False

        async def on_timeout(self):
            if self.completed:
                return

            self.completed = True
            self.expired = True
            if self.confirmation_message is None:
                return

            try:
                await self.confirmation_message.edit(
                    content="انتهت مهلة تأكيد إلغاء الدعوة. لم تُحذف الدعوة.",
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not mark bot invite confirmation expired.")

        async def _reject_unavailable_confirmation(
            self,
            interaction: discord.Interaction,
        ) -> bool:
            if self.completed:
                content = (
                    "انتهت مهلة تأكيد إلغاء الدعوة. لم تُحذف الدعوة."
                    if self.expired
                    else "تمت معالجة هذا التأكيد مسبقًا."
                )
                await interaction.response.edit_message(
                    content=content,
                    view=None,
                )
                return True

            if time.monotonic() >= self.expires_at:
                self.completed = True
                self.expired = True
                await interaction.response.edit_message(
                    content="انتهت مهلة تأكيد إلغاء الدعوة. لم تُحذف الدعوة.",
                    view=None,
                )
                return True

            return False

        @discord.ui.button(
            label="تأكيد إلغاء الدعوة",
            style=discord.ButtonStyle.danger,
        )
        async def confirm(self, interaction: discord.Interaction, _button):
            if not await authorize_invite_interaction(interaction):
                return
            if await self._reject_unavailable_confirmation(interaction):
                return
            self.completed = True

            guild = bot.get_guild(self.guild_id)
            if guild is None:
                await interaction.response.edit_message(
                    content="لم يعد البوت في هذا السيرفر؛ لم تُحذف الدعوة.",
                    view=None,
                )
                return

            bot_id = getattr(getattr(bot, "user", None), "id", None)
            if bot_id is None:
                await interaction.response.edit_message(
                    content="تعذر التحقق من ملكية الدعوة؛ لم تُحذف.",
                    view=None,
                )
                return

            try:
                current_invites = await guild.invites()
            except discord.Forbidden:
                await interaction.response.edit_message(
                    content="لا أستطيع فحص الدعوات في هذا السيرفر؛ لم تُحذف.",
                    view=None,
                )
                return
            except discord.HTTPException:
                logger.exception(
                    "Could not verify invite %s in guild %s before deletion.",
                    self.invite_code,
                    guild.id,
                )
                await interaction.response.edit_message(
                    content="تعذر التحقق من الدعوة بسبب خطأ من Discord؛ لم تُحذف.",
                    view=None,
                )
                return

            invite = next(
                (
                    candidate
                    for candidate in current_invites
                    if getattr(candidate, "code", None) == self.invite_code
                    and getattr(
                        getattr(candidate, "inviter", None),
                        "id",
                        None,
                    )
                    == bot_id
                    and not getattr(candidate, "revoked", False)
                ),
                None,
            )
            if invite is None:
                await interaction.response.edit_message(
                    content="لم تعد الدعوة نشطة أو لم تعد من إنشاء البوت؛ لم تُحذف.",
                    view=None,
                )
                return

            try:
                await invite.delete(
                    reason=f"Invite revocation requested by developer {developer_id}"
                )
            except discord.NotFound:
                result = "كانت الدعوة قد أُلغيت بالفعل."
            except discord.Forbidden:
                result = "رفض Discord إلغاء الدعوة. تحقق من صلاحية Manage Server."
            except discord.HTTPException:
                logger.exception(
                    "Could not revoke bot-created invite %s in guild %s.",
                    self.invite_code,
                    guild.id,
                )
                result = "تعذر إلغاء الدعوة بسبب خطأ من Discord."
            else:
                result = "أُلغيت الدعوة بنجاح."

            await interaction.response.edit_message(
                content=(
                    f"{result}\nالسيرفر: **"
                    f"{discord.utils.escape_markdown(self.guild_name)}** "
                    f"| معرّف الدعوة: `{self.invite_code}`"
                ),
                view=None,
            )

        @discord.ui.button(label="إبقاء الدعوة", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, _button):
            if not await authorize_invite_interaction(interaction):
                return
            if await self._reject_unavailable_confirmation(interaction):
                return
            self.completed = True
            await interaction.response.edit_message(
                content="لم يتم إلغاء الدعوة.",
                view=None,
            )

    class BotInviteListView(discord.ui.View):
        def __init__(self, entries, scan_errors: list[str]):
            super().__init__(timeout=300)
            self.entries = entries
            self.scan_errors = scan_errors
            self.page = 0
            self.page_size = 8
            self.message = None
            self.expired = False
            self._add_page_controls()

        async def on_timeout(self):
            self.expired = True
            if self.message is None:
                return

            try:
                await self.message.edit(
                    content="انتهت صلاحية قائمة الدعوات. أرسل الأمر مجددًا لعرض القائمة.",
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not mark private bot invite list expired.")

        async def _reject_expired_interaction(
            self,
            interaction: discord.Interaction,
        ) -> bool:
            if not self.expired:
                return False

            await interaction.response.send_message(
                "انتهت صلاحية قائمة الدعوات. أرسل الأمر مجددًا لعرض القائمة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        def _add_page_controls(self):
            self.clear_items()
            page_count = max(
                1,
                (len(self.entries) + self.page_size - 1) // self.page_size,
            )
            page_entries = self.entries[
                self.page * self.page_size : (self.page + 1) * self.page_size
            ]
            options = []
            for guild, invite in page_entries:
                code = str(getattr(invite, "code", ""))
                guild_name = discord.utils.escape_markdown(
                    str(getattr(guild, "name", "سيرفر")).replace("\n", " ")
                )
                options.append(
                    discord.SelectOption(
                        label=f"{guild_name[:55]} · {code}"[:100],
                        value=f"{guild.id}:{code}"[:100],
                        description=f"السيرفر {guild.id}"[:100],
                    )
                )

            if options:
                select = discord.ui.Select(
                    placeholder="اختر دعوة تريد إلغاءها",
                    min_values=1,
                    max_values=1,
                    options=options,
                )

                async def select_callback(interaction: discord.Interaction):
                    if not await authorize_invite_interaction(interaction):
                        return
                    if await self._reject_expired_interaction(interaction):
                        return
                    selected_key = select.values[0]
                    selected = next(
                        (
                            (guild, invite)
                            for guild, invite in self.entries
                            if f"{guild.id}:{getattr(invite, 'code', '')}"
                            == selected_key
                        ),
                        None,
                    )
                    if selected is None:
                        await interaction.response.edit_message(
                            content="تعذر العثور على الدعوة المحددة؛ لم تُحذف.",
                            view=None,
                        )
                        return

                    guild, invite = selected
                    code = str(getattr(invite, "code", ""))
                    guild_name = str(getattr(guild, "name", "سيرفر"))
                    confirmation_view = BotInviteConfirmationView(
                        guild.id,
                        code,
                        guild_name,
                    )
                    confirmation_view.confirmation_message = getattr(
                        interaction,
                        "message",
                        None,
                    )
                    await interaction.response.edit_message(
                        content=(
                            f"هل تريد إلغاء الدعوة `{code}` في السيرفر "
                            f"**{discord.utils.escape_markdown(guild_name)}**؟\n"
                            "لن تُحذف إلا بعد الضغط على «تأكيد إلغاء الدعوة».\n"
                            f"مهلة التأكيد {BOT_INVITE_CONFIRMATION_TIMEOUT_SECONDS} "
                            "ثانية؛ بعدها تنتهي صلاحية الأزرار."
                        ),
                        view=confirmation_view,
                    )

                select.callback = select_callback
                self.add_item(select)

            previous_button = discord.ui.Button(
                label="السابق",
                style=discord.ButtonStyle.secondary,
                disabled=self.page <= 0,
            )
            next_button = discord.ui.Button(
                label="التالي",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= page_count - 1,
            )

            async def previous_callback(interaction: discord.Interaction):
                if not await authorize_invite_interaction(interaction):
                    return
                if await self._reject_expired_interaction(interaction):
                    return
                self.page -= 1
                self._add_page_controls()
                await interaction.response.edit_message(
                    content=render_bot_invite_page(
                        self.entries,
                        self.page,
                        self.scan_errors,
                    ),
                    view=self,
                )

            async def next_callback(interaction: discord.Interaction):
                if not await authorize_invite_interaction(interaction):
                    return
                if await self._reject_expired_interaction(interaction):
                    return
                self.page += 1
                self._add_page_controls()
                await interaction.response.edit_message(
                    content=render_bot_invite_page(
                        self.entries,
                        self.page,
                        self.scan_errors,
                    ),
                    view=self,
                )

            previous_button.callback = previous_callback
            next_button.callback = next_callback
            self.add_item(previous_button)
            self.add_item(next_button)

    class DeveloperInviteReportConfirmationView(discord.ui.View):
        def __init__(self, preview_entries, preview_summary_lines):
            super().__init__(timeout=180)
            self.preview_entries = preview_entries
            self.preview_summary_lines = preview_summary_lines
            self.preview_status_lines = self._build_preview_status_lines()
            self.preview_pages = self._build_preview_pages()
            self.preview_page = 0
            self.new_invite_count = sum(
                invite is None and inspection_error is None and channel is not None
                for _guild, invite, inspection_error, channel in preview_entries
            )
            self.exceeds_batch_limit = (
                self.new_invite_count > MAX_PERMANENT_INVITES_PER_BATCH
            )
            self.completed = False
            self.expired = False
            self.preview_message = None
            if self.exceeds_batch_limit:
                for child in self.children:
                    if (
                        isinstance(child, discord.ui.Button)
                        and child.label == "تأكيد إنشاء الروابط"
                    ):
                        child.disabled = True

            if len(self.preview_pages) > 1:
                previous_button = discord.ui.Button(
                    label="السابق",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                    disabled=True,
                )
                next_button = discord.ui.Button(
                    label="التالي",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                )

                async def change_page(interaction, offset):
                    if not await authorize_invite_interaction(interaction):
                        return
                    if self.completed:
                        await interaction.response.send_message(
                            "انتهت هذه المعاينة بالفعل.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return

                    self.preview_page = min(
                        max(0, self.preview_page + offset),
                        len(self.preview_pages) - 1,
                    )
                    previous_button.disabled = self.preview_page == 0
                    next_button.disabled = (
                        self.preview_page == len(self.preview_pages) - 1
                    )
                    await interaction.response.edit_message(
                        content=self._render_preview_page(),
                        view=self,
                    )

                async def previous_page(interaction):
                    await change_page(interaction, -1)

                async def next_page(interaction):
                    await change_page(interaction, 1)

                previous_button.callback = previous_page
                next_button.callback = next_page
                self.add_item(previous_button)
                self.add_item(next_button)

        def _build_preview_status_lines(self):
            lines = []
            for guild, invite, inspection_error, channel in self.preview_entries:
                name = discord.utils.escape_markdown(
                    str(getattr(guild, "name", "سيرفر")).replace("\n", " ")
                )[:80]
                if inspection_error is not None:
                    cause = inspection_error.__cause__
                    if isinstance(cause, discord.Forbidden):
                        status = "تعذر الفحص: صلاحية Manage Server مطلوبة"
                    elif isinstance(cause, discord.HTTPException):
                        status = "تعذر الفحص: خطأ مؤقت من Discord"
                    else:
                        status = "تعذر الفحص: سبب غير معروف"
                elif invite is not None:
                    status = "دعوة دائمة قابلة لإعادة الاستخدام موجودة"
                elif channel is not None:
                    status = "جاهز لإنشاء دعوة دائمة جديدة"
                else:
                    status = "لا توجد قناة بصلاحية إنشاء الدعوات"
                lines.append(f"• {name} | ID: `{guild.id}` | {status}")
            if not lines:
                lines.append("لا توجد سيرفرات متصلة بالبوت.")
            return lines

        def _format_preview_page(self, status_lines, page_number, page_count):
            return "\n".join(
                [
                    *self.preview_summary_lines,
                    "",
                    f"حالة كل سيرفر (صفحة {page_number}/{page_count}):",
                    *status_lines,
                ]
            )

        @staticmethod
        def _discord_message_length(content):
            return len(content.encode("utf-16-le")) // 2

        def _build_preview_pages(self):
            max_message_length = 1850
            pages = []
            current_page = []
            for line in self.preview_status_lines:
                candidate_page = [*current_page, line]
                candidate_content = self._format_preview_page(
                    candidate_page,
                    9999,
                    9999,
                )
                if (
                    current_page
                    and self._discord_message_length(candidate_content)
                    > max_message_length
                ):
                    pages.append(current_page)
                    current_page = [line]
                else:
                    current_page = candidate_page
            if current_page:
                pages.append(current_page)
            return pages or [[]]

        def _render_preview_page(self):
            return self._format_preview_page(
                self.preview_pages[self.preview_page],
                self.preview_page + 1,
                len(self.preview_pages),
            )

        async def on_timeout(self):
            if self.completed:
                return

            self.completed = True
            self.expired = True
            if self.preview_message is None:
                return

            try:
                await self.preview_message.edit(
                    content=(
                        "انتهى وقت المعاينة. لم يبدأ إنشاء الروابط "
                        "ولم يتم إنشاء أي رابط."
                    ),
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not mark developer invite preview expired.")

        @discord.ui.button(
            label="تأكيد إنشاء الروابط",
            style=discord.ButtonStyle.success,
        )
        async def confirm(self, interaction: discord.Interaction, _button):
            if not await authorize_invite_interaction(interaction):
                return
            if self.completed:
                await interaction.response.send_message(
                    (
                        "انتهى وقت المعاينة. لم يبدأ إنشاء الروابط "
                        "ولم يتم إنشاء أي رابط."
                        if self.expired
                        else "انتهت هذه المعاينة بالفعل."
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if self.exceeds_batch_limit:
                await interaction.response.send_message(
                    (
                        "تتجاوز هذه المعاينة الحد الأقصى وهو "
                        f"{MAX_PERMANENT_INVITES_PER_BATCH} روابط جديدة لكل دفعة. "
                        "قلل النطاق وأنشئ معاينة جديدة؛ لم يتم إنشاء أي رابط."
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            self.completed = True
            await interaction.response.edit_message(
                content="تم التأكيد. يجري تجهيز تقرير الدعوات الآن.",
                view=None,
            )

            report_lines = []
            confirmation_retryable = {}
            if not self.preview_entries:
                report_lines.append("البوت غير موجود حاليًا في أي سيرفر.")

            for (
                guild,
                preview_invite,
                inspection_error,
                creation_channel,
            ) in self.preview_entries:
                name = discord.utils.escape_markdown(
                    str(getattr(guild, "name", "سيرفر")).replace("\n", " ")
                )[:200]
                if inspection_error is not None:
                    if _is_temporary_invite_inspection_error(inspection_error):
                        confirmation_retryable[guild.id] = guild
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        f"{_invite_inspection_failure_reason(inspection_error)} "
                        "لم يُنشأ رابط جديد."
                    )
                    continue
                if preview_invite is None and creation_channel is None:
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        "لا توجد قناة يملك البوت فيها صلاحية إنشاء الدعوات؛ "
                        "لم يُنشأ رابط جديد."
                    )
                    continue
                if preview_invite is not None:
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | {preview_invite.url}"
                    )
                    continue

                try:
                    invite = await _get_or_create_permanent_invite(
                        guild,
                        creation_channel,
                        interaction.user.id,
                        require_inspection=True,
                    )
                except InviteInspectionError as error:
                    if _is_temporary_invite_inspection_error(error):
                        confirmation_retryable[guild.id] = guild
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        f"{_invite_inspection_failure_reason(error)} "
                        "لم يُنشأ رابط جديد."
                    )
                except NoInviteChannelError:
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        "لا توجد قناة يملك البوت فيها صلاحية إنشاء الدعوات؛ "
                        "لم يُنشأ رابط جديد."
                    )
                except discord.Forbidden:
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        "لا يملك البوت صلاحية إنشاء دعوة."
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not create an invite for guild %s.",
                        guild.id,
                    )
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        "تعذر إنشاء دعوة بسبب خطأ من Discord."
                    )
                else:
                    if invite is None:
                        report_lines.append(
                            f"• {name} | ID: {guild.id} | "
                            "لا توجد دعوة دائمة قابلة لإعادة الاستخدام؛ "
                            "لم يُنشأ رابط جديد."
                        )
                    else:
                        report_lines.append(
                            f"• {name} | ID: {guild.id} | {invite.url}"
                        )

            replace_retryable_inspection_guilds(confirmation_retryable)
            report_chunks = _developer_invite_report_chunks(
                report_lines,
                create_missing=True,
            )
            completion_view = None
            try:
                for message in report_chunks:
                    await interaction.user.send(
                        message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except discord.Forbidden:
                logger.info(
                    "Developer invite report delivery was blocked; keeping a private recovery copy."
                )
                report_lines.insert(
                    0,
                    "تعذر إرسال التقرير إلى الخاص بالكامل. هذه نسخة خاصة بالنتائج؛ "
                    "يمكن استعادتها لاحقًا عبر `!رابط_سيرفر استعادة`.",
                )
                report_chunks = _developer_invite_report_chunks(
                    report_lines,
                    create_missing=True,
                )
                if not pending_reports.add_report(report_chunks):
                    report_lines.insert(
                        0,
                        "تعذر إرسال التقرير إلى الخاص وتعذر حفظ نسخة الاستعادة. "
                        "النتائج أدناه متاحة في هذه المحادثة الخاصة فقط؛ "
                        "احفظها قبل إغلاقها.",
                    )
                    report_chunks = _developer_invite_report_chunks(
                        report_lines,
                        create_missing=True,
                    )
                completion_message = report_chunks[0]
                if len(report_chunks) > 1:
                    completion_view = DeveloperInviteReportPagesView(report_chunks)
            except discord.HTTPException:
                logger.exception(
                    "Could not deliver the confirmed private developer invite report."
                )
                report_lines.insert(
                    0,
                    "تعذر إرسال التقرير إلى الخاص بسبب خطأ من Discord. "
                    "هذه نسخة خاصة بالنتائج؛ يمكن استعادتها لاحقًا عبر "
                    "`!رابط_سيرفر استعادة`.",
                )
                report_chunks = _developer_invite_report_chunks(
                    report_lines,
                    create_missing=True,
                )
                if not pending_reports.add_report(report_chunks):
                    report_lines.insert(
                        0,
                        "تعذر إرسال التقرير إلى الخاص وتعذر حفظ نسخة الاستعادة. "
                        "النتائج أدناه متاحة في هذه المحادثة الخاصة فقط؛ "
                        "احفظها قبل إغلاقها.",
                    )
                    report_chunks = _developer_invite_report_chunks(
                        report_lines,
                        create_missing=True,
                    )
                completion_message = report_chunks[0]
                if len(report_chunks) > 1:
                    completion_view = DeveloperInviteReportPagesView(report_chunks)
            else:
                completion_message = "اكتمل الإجراء وأُرسل التقرير إلى الخاص."

            try:
                report_message = await interaction.edit_original_response(
                    content=completion_message,
                    view=completion_view,
                )
                if completion_view is not None:
                    completion_view.message = report_message
            except discord.HTTPException:
                logger.exception(
                    "Could not show the confirmed invite report in its private confirmation."
                )

        @discord.ui.button(
            label="إلغاء",
            style=discord.ButtonStyle.secondary,
        )
        async def cancel(self, interaction: discord.Interaction, _button):
            if not await authorize_invite_interaction(interaction):
                return
            if self.completed:
                await interaction.response.send_message(
                    "انتهت هذه المعاينة بالفعل.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            self.completed = True
            await interaction.response.edit_message(
                content="أُلغي إنشاء الروابط. لم يتم إنشاء أي رابط.",
                view=None,
            )

    @bot.command(name="رابط", aliases=["link", "دعوة", "invite"])
    async def server_invite(ctx):
        """Create or show a permanent invite for the current server."""
        guild = ctx.guild
        if guild is None:
            await reply(ctx, "هذا الأمر يعمل داخل السيرفرات فقط.")
            return

        try:
            invite = await _get_or_create_permanent_invite(
                guild,
                ctx.channel,
                ctx.author.id,
            )
        except NoInviteChannelError:
            await reply(
                ctx,
                "لم أجد قناة أملك فيها صلاحية **Create Instant Invite**.",
            )
            return
        except discord.Forbidden:
            await reply(
                ctx,
                "رفض Discord إنشاء الدعوة. تحقق من صلاحية **Create Instant Invite** "
                "للبوت في إحدى القنوات.",
            )
            return
        except discord.HTTPException:
            logger.exception("Could not create an invite for guild %s.", guild.id)
            await reply(ctx, "تعذر إنشاء الدعوة بسبب خطأ من Discord. حاول لاحقًا.")
            return

        view = discord.ui.View(timeout=300)
        copy_button = discord.ui.Button(
            label="عرض الرابط للنسخ",
            style=discord.ButtonStyle.success,
            emoji="📋",
        )

        async def copy_callback(interaction: discord.Interaction):
            await interaction.response.send_message(
                f"رابط دعوة السيرفر:\n```{invite.url}```",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        copy_button.callback = copy_callback
        view.add_item(copy_button)
        view.add_item(
            discord.ui.Button(
                label="فتح الدعوة",
                style=discord.ButtonStyle.link,
                url=invite.url,
                emoji="🔗",
            )
        )
        await ctx.send(
            embed=_invite_embed(guild, invite, "دعوة سيرفر {guild_name}"),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_invite_batch_preview(
        author,
        guilds,
        *,
        scope_label: str,
        over_limit_hint: str,
    ) -> str:
        preview_entries = []
        for guild in guilds:
            try:
                invite = await _find_permanent_invite(
                    guild,
                    require_inspection=True,
                )
            except InviteInspectionError as error:
                preview_entries.append((guild, None, error, None))
            else:
                creation_channel = (
                    _find_invite_channel(guild) if invite is None else None
                )
                preview_entries.append((guild, invite, None, creation_channel))

        replace_retryable_inspection_guilds(
            {
                guild.id: guild
                for guild, _invite, inspection_error, _channel in preview_entries
                if inspection_error is not None
                and _is_temporary_invite_inspection_error(inspection_error)
            }
        )

        missing_count = sum(
            invite is None and inspection_error is None
            for _guild, invite, inspection_error, _channel in preview_entries
        )
        ready_count = sum(
            invite is None
            and inspection_error is None
            and channel is not None
            for _guild, invite, inspection_error, channel in preview_entries
        )
        blocked_count = sum(
            invite is None
            and inspection_error is None
            and channel is None
            for _guild, invite, inspection_error, channel in preview_entries
        )
        unknown_count = sum(
            inspection_error is not None
            for _guild, _invite, inspection_error, _channel in preview_entries
        )
        permission_error_count = sum(
            isinstance(inspection_error.__cause__, discord.Forbidden)
            for _guild, _invite, inspection_error, _channel in preview_entries
            if inspection_error is not None
        )
        discord_error_count = sum(
            isinstance(inspection_error.__cause__, discord.HTTPException)
            and not isinstance(inspection_error.__cause__, discord.Forbidden)
            for _guild, _invite, inspection_error, _channel in preview_entries
            if inspection_error is not None
        )

        preview_lines = [
            "معاينة إنشاء روابط الدعوة الدائمة",
            f"النطاق: {scope_label}",
            (
                "السيرفرات التي تحتاج رابطًا دائمًا جديدًا: "
                f"**{missing_count}** من أصل **{len(guilds)}**."
            ),
            f"يمكن إنشاء رابط الآن في **{ready_count}** سيرفر.",
        ]
        if ready_count > MAX_PERMANENT_INVITES_PER_BATCH:
            preview_lines.append(
                "تتجاوز هذه المعاينة الحد الأقصى وهو "
                f"**{MAX_PERMANENT_INVITES_PER_BATCH}** روابط دائمة جديدة لكل دفعة. "
                f"زر التأكيد معطل. {over_limit_hint}"
            )
        else:
            preview_lines.append("لن يبدأ الإنشاء إلا بعد تأكيدك.")
        if blocked_count:
            preview_lines.append(
                "لا توجد قناة بصلاحية إنشاء الدعوات في "
                f"**{blocked_count}** من السيرفرات التي تحتاج رابطًا؛ "
                "لن تُنشأ فيها روابط بعد التأكيد."
            )
        if unknown_count:
            preview_lines.append(
                f"تعذر فحص {unknown_count} سيرفر: "
                f"{permission_error_count} بسبب نقص صلاحية Manage Server، "
                f"و{discord_error_count} بسبب أخطاء مؤقتة من Discord؛ "
                "لن تُنشأ فيها روابط."
            )

        preview_view = DeveloperInviteReportConfirmationView(
            preview_entries,
            preview_lines,
        )
        try:
            preview_view.preview_message = await author.send(
                preview_view._render_preview_page(),
                view=preview_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            return (
                "تعذر إرسال المعاينة إلى الخاص. افتح استقبال الرسائل الخاصة من "
                "البوت ثم أعد المحاولة. لم تُنشأ روابط."
            )
        except discord.HTTPException:
            logger.exception("Could not deliver the private developer invite preview.")
            return "تعذر إرسال المعاينة إلى الخاص بسبب خطأ من Discord. لم تُنشأ روابط."
        return "أرسلت المعاينة إلى الخاص. لم تُنشأ روابط بعد."

    class DeveloperInviteBatchPickerView(discord.ui.View):
        PAGE_SIZE = 25

        def __init__(self, guilds):
            super().__init__(timeout=300)
            self.guilds = sorted(
                guilds,
                key=lambda guild: (
                    str(getattr(guild, "name", "")).casefold(),
                    guild.id,
                ),
            )
            self.selected_ids: set[int] = set()
            self.page = 0
            self.finished = False
            self.expired = False
            self.message = None
            self._add_controls()

        @property
        def page_count(self):
            return max(1, math.ceil(len(self.guilds) / self.PAGE_SIZE))

        def _page_guilds(self):
            start = self.page * self.PAGE_SIZE
            return self.guilds[start : start + self.PAGE_SIZE]

        def _render_content(self):
            return (
                "اختر السيرفرات المطلوبة من القائمة. يبقى اختيارك محفوظًا عند "
                "التنقل بين الصفحات.\n"
                f"الصفحة **{self.page + 1}/{self.page_count}** — "
                f"المحدد **{len(self.selected_ids)}**."
            )

        async def _authorize_active_interaction(self, interaction) -> bool:
            if not await authorize_invite_interaction(interaction):
                return False
            if not self.expired and not self.finished:
                return True
            await interaction.response.send_message(
                (
                    "انتهت صلاحية قائمة الاختيار؛ افتح قائمة جديدة."
                    if self.expired
                    else "انتهت قائمة الاختيار بالفعل."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False

        def _add_controls(self):
            self.clear_items()
            page_guilds = self._page_guilds()
            options = [
                discord.SelectOption(
                    label=(
                        str(getattr(guild, "name", "سيرفر"))
                        .replace("\n", " ")
                        .strip()[:100]
                        or "سيرفر"
                    ),
                    value=str(guild.id),
                    description=f"ID: {guild.id}",
                    default=guild.id in self.selected_ids,
                )
                for guild in page_guilds
            ]
            guild_select = discord.ui.Select(
                placeholder="اختر سيرفرًا أو أكثر من هذه الصفحة",
                min_values=0,
                max_values=max(1, len(options)),
                options=options,
                row=0,
            )

            async def select_guilds(interaction):
                if not await self._authorize_active_interaction(interaction):
                    return
                current_page_ids = {guild.id for guild in page_guilds}
                selected_values = set(guild_select.values)
                self.selected_ids.difference_update(current_page_ids)
                self.selected_ids.update(
                    guild_id
                    for guild_id in current_page_ids
                    if str(guild_id) in selected_values
                )
                self._add_controls()
                await interaction.response.edit_message(
                    content=self._render_content(),
                    view=self,
                )

            guild_select.callback = select_guilds
            self.add_item(guild_select)

            if self.page_count > 1:
                previous_button = discord.ui.Button(
                    label="السابق",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                    disabled=self.page == 0,
                )
                next_button = discord.ui.Button(
                    label="التالي",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                    disabled=self.page == self.page_count - 1,
                )

                async def change_page(interaction, offset):
                    if not await self._authorize_active_interaction(interaction):
                        return
                    self.page = min(
                        max(0, self.page + offset),
                        self.page_count - 1,
                    )
                    self._add_controls()
                    await interaction.response.edit_message(
                        content=self._render_content(),
                        view=self,
                    )

                async def previous_page(interaction):
                    await change_page(interaction, -1)

                async def next_page(interaction):
                    await change_page(interaction, 1)

                previous_button.callback = previous_page
                next_button.callback = next_page
                self.add_item(previous_button)
                self.add_item(next_button)

            preview_button = discord.ui.Button(
                label="معاينة المختارة",
                style=discord.ButtonStyle.success,
                row=2,
            )

            async def preview_selected(interaction):
                if not await self._authorize_active_interaction(interaction):
                    return
                if not self.selected_ids:
                    await interaction.response.send_message(
                        "حدد سيرفرًا واحدًا على الأقل أولًا.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                connected_guilds = {guild.id: guild for guild in bot.guilds}
                disconnected_ids = self.selected_ids - set(connected_guilds)
                if disconnected_ids:
                    missing = ", ".join(
                        str(guild_id) for guild_id in sorted(disconnected_ids)
                    )
                    await interaction.response.send_message(
                        "انقطع اتصال البوت ببعض السيرفرات المحددة "
                        f"({missing}). افتح قائمة اختيار جديدة ثم أعد المحاولة؛ "
                        "لم تُنشأ روابط.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                selected_guilds = [
                    connected_guilds[guild.id]
                    for guild in self.guilds
                    if guild.id in self.selected_ids
                ]
                self.finished = True
                await interaction.response.edit_message(
                    content="تم استلام الاختيار؛ يجري إعداد معاينة خاصة.",
                    view=None,
                )
                result = await send_invite_batch_preview(
                    interaction.user,
                    selected_guilds,
                    scope_label=(
                        f"السيرفرات المحددة من القائمة ({len(selected_guilds)})"
                    ),
                    over_limit_hint=(
                        "اختر عددًا أقل من السيرفرات باستخدام "
                        "`!رابط_سيرفر اختيار` ثم أنشئ معاينة جديدة."
                    ),
                )
                try:
                    await interaction.edit_original_response(
                        content=result,
                        view=None,
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not update the private developer invite picker."
                    )

            async def cancel_picker(interaction):
                if not await self._authorize_active_interaction(interaction):
                    return
                self.finished = True
                await interaction.response.edit_message(
                    content="أُلغيت قائمة الاختيار. لم تُفحص الدعوات ولم تُنشأ روابط.",
                    view=None,
                )

            preview_button.callback = preview_selected
            cancel_button = discord.ui.Button(
                label="إلغاء",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            cancel_button.callback = cancel_picker
            self.add_item(preview_button)
            self.add_item(cancel_button)

        async def on_timeout(self):
            if self.finished:
                return
            self.finished = True
            self.expired = True
            for child in self.children:
                child.disabled = True
            if self.message is None:
                return
            try:
                await self.message.edit(
                    content=(
                        "انتهت صلاحية قائمة الاختيار. لم تُفحص الدعوات "
                        "ولم تُنشأ روابط."
                    ),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not mark the developer invite picker expired.")

    @bot.command(name="رابط_سيرفر", aliases=["guild_invite", "دعوة_سيرفر"])
    async def developer_server_invite(ctx, *options: str):
        """Privately send the developer a server list or selected server invites."""
        if ctx.author.id != developer_id:
            await reply(ctx, "هذا الأمر خاص بمطور البوت فقط.")
            return

        if len(options) == 1 and str(options[0]).strip().casefold() in {
            "اختيار",
            "قائمة",
            "pick",
            "select",
        }:
            guilds = list(bot.guilds)
            if not guilds:
                await reply(ctx, "لا توجد سيرفرات متصلة بالبوت للاختيار منها.")
                return
            picker_view = DeveloperInviteBatchPickerView(guilds)
            try:
                picker_view.message = await ctx.author.send(
                    picker_view._render_content(),
                    view=picker_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                await reply(
                    ctx,
                    "تعذر إرسال قائمة الاختيار إلى الخاص. افتح استقبال الرسائل "
                    "الخاصة من البوت ثم أعد المحاولة.",
                )
                return
            except discord.HTTPException:
                logger.exception("Could not deliver the private developer invite picker.")
                await reply(
                    ctx,
                    "تعذر إرسال قائمة الاختيار إلى الخاص بسبب خطأ من Discord.",
                )
                return
            await reply(ctx, "أرسلت قائمة اختيار السيرفرات إلى الخاص.")
            return

        discard_options = {
            "حذف_التقارير",
            "مسح_التقارير",
            "discard",
            "clear",
        }
        if (
            len(options) == 1
            and str(options[0]).strip().casefold() in discard_options
        ):
            async with pending_reports_lock:
                try:
                    discarded_count = pending_reports.discard_all()
                except OSError:
                    logger.exception(
                        "Could not discard pending developer invite reports."
                    )
                    await reply(
                        ctx,
                        "تعذر حذف تقارير الدعوات المعلقة؛ بقيت محفوظة.",
                    )
                    return

                if not discarded_count:
                    await reply(ctx, "لا توجد تقارير دعوات معلقة للحذف.")
                    return

                await reply(
                    ctx,
                    f"تم حذف {discarded_count} نسخة استعادة من تقارير الدعوات "
                    "المعلقة. لم تتغير الدعوات نفسها ولا يمكن استعادة هذه النسخ.",
                )
            return

        if len(options) == 1 and str(options[0]).strip().casefold() in {
            "استعادة",
            "recover",
        }:
            async with pending_reports_lock:
                reports_to_send = pending_reports.snapshot()
                if not reports_to_send:
                    await reply(ctx, "لا توجد تقارير دعوات معلقة للاستعادة.")
                    return

                reports_sent = 0
                for report_chunks in reports_to_send:
                    try:
                        for message in report_chunks:
                            await ctx.author.send(
                                message,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                    except discord.Forbidden:
                        await reply(
                            ctx,
                            "تعذر إرسال التقرير إلى الخاص. افتح استقبال الرسائل "
                            "الخاصة من البوت ثم أعد `!رابط_سيرفر استعادة`؛ "
                            "بقي التقرير محفوظًا.",
                        )
                        return
                    except discord.HTTPException:
                        logger.exception(
                            "Could not resend a pending private invite report."
                        )
                        await reply(
                            ctx,
                            "تعذر إرسال التقرير إلى الخاص بسبب خطأ من Discord. "
                            "أعد `!رابط_سيرفر استعادة` لاحقًا؛ "
                            "بقي التقرير محفوظًا.",
                        )
                        return

                    try:
                        pending_reports.remove_report(report_chunks)
                    except OSError:
                        logger.exception(
                            "Could not remove a delivered invite report from recovery."
                        )
                        await reply(
                            ctx,
                            "أُرسل التقرير إلى الخاص، لكن تعذر تحديث قائمة "
                            "الاستعادة؛ قد يُرسل مرة أخرى عند الاستعادة التالية.",
                        )
                        return
                    reports_sent += 1

                await reply(
                    ctx,
                    f"أُرسلت {reports_sent} تقارير معلقة إلى الخاص.",
                )
            return

        retry_options = {
            "إعادة_فحص",
            "اعادة_فحص",
            "retry",
            "retry_temporary",
            "retry-temporary",
        }
        retry_option_positions = [
            index
            for index, option in enumerate(options)
            if str(option).strip().casefold() in retry_options
        ]
        if retry_option_positions:
            selected_guild_id = None
            remaining_options = [
                str(option).strip()
                for index, option in enumerate(options)
                if index not in retry_option_positions
            ]
            if (
                len(retry_option_positions) != 1
                or len(remaining_options) > 1
                or (
                    remaining_options
                    and not remaining_options[0].isdecimal()
                )
            ):
                await reply(
                    ctx,
                    "الاستخدام: `!رابط_سيرفر إعادة_فحص [ID]`. "
                    "يعيد فحص السيرفرات التي فشل فحص دعواتها مؤقتًا فقط.",
                )
                return
            if remaining_options:
                selected_guild_id = int(remaining_options[0])

            retry_candidates = dict(retryable_inspection_guilds)
            if selected_guild_id is not None:
                guild = retry_candidates.get(selected_guild_id)
                if guild is None:
                    await reply(
                        ctx,
                        "هذا السيرفر غير موجود ضمن فحوص الدعوات التي فشلت مؤقتًا؛ "
                        "لم يُعَد فحصه.",
                    )
                    return
                retry_candidates = {selected_guild_id: guild}
            elif not retry_candidates:
                await reply(
                    ctx,
                    "لا توجد فحوص دعوات فشلت مؤقتًا لإعادتها. "
                    "أجرِ تقريرًا جديدًا أولًا.",
                )
                return

            retry_started_at = time.monotonic()
            for guild_id, retry_at in tuple(retry_cooldown_until.items()):
                if retry_at <= retry_started_at:
                    retry_cooldown_until.pop(guild_id, None)

            cooldown_guilds = {}
            cooldown_wait_seconds = {}
            candidates_to_check = {}
            for guild_id, guild in retry_candidates.items():
                retry_at = retry_cooldown_until.get(guild_id)
                if retry_at is not None and retry_at > retry_started_at:
                    cooldown_guilds[guild_id] = guild
                    cooldown_wait_seconds[guild_id] = max(
                        1,
                        math.ceil(retry_at - retry_started_at),
                    )
                else:
                    candidates_to_check[guild_id] = guild

            cooldown_summary = "، ".join(
                f"ID {guild_id}: {seconds} ثانية"
                for guild_id, seconds in cooldown_wait_seconds.items()
            )
            if not candidates_to_check:
                await reply(
                    ctx,
                    "لم يُعَد فحص أي سيرفر لأنها ضمن مهلة الانتظار. "
                    f"أعد المحاولة بعد: {cooldown_summary}. "
                    "لم يُرسل أي طلب إلى Discord.",
                )
                return

            cooldown_notice = (
                f" ما زالت هذه السيرفرات ضمن مهلة الانتظار: {cooldown_summary}."
                if cooldown_wait_seconds
                else ""
            )
            retry_report_lines = [
                f"• ID: {guild_id} | لم يُعَد فحصه لتجنب تكرار الطلبات؛ "
                f"أعد المحاولة بعد {seconds} ثانية."
                for guild_id, seconds in cooldown_wait_seconds.items()
            ]
            still_retryable = dict(cooldown_guilds)
            checked_count = 0
            recovered_inspection_count = 0
            remaining_inspection_failure_count = 0
            not_rechecked_count = len(cooldown_wait_seconds)
            for retry_guild_id, guild in candidates_to_check.items():
                guild_lock = retry_guild_locks.setdefault(
                    retry_guild_id,
                    asyncio.Lock(),
                )
                async with guild_lock:
                    retry_at = retry_cooldown_until.get(retry_guild_id)
                    retry_now = time.monotonic()
                    if retry_at is not None and retry_at > retry_now:
                        seconds = max(1, math.ceil(retry_at - retry_now))
                        cooldown_guilds[retry_guild_id] = guild
                        cooldown_wait_seconds[retry_guild_id] = seconds
                        still_retryable[retry_guild_id] = guild
                        not_rechecked_count += 1
                        retry_report_lines.append(
                            f"• ID: {retry_guild_id} | لم يُعَد فحصه لتجنب "
                            f"تكرار الطلبات؛ أعد المحاولة بعد {seconds} ثانية."
                        )
                        continue
                    if retry_at is not None:
                        retry_cooldown_until.pop(retry_guild_id, None)

                    current_guild = bot.get_guild(retry_guild_id)
                    if current_guild is None:
                        retry_cooldown_until.pop(retry_guild_id, None)
                        not_rechecked_count += 1
                        retry_report_lines.append(
                            f"• ID: {retry_guild_id} | لم يعد البوت موجودًا في هذا السيرفر."
                        )
                        continue

                    name = discord.utils.escape_markdown(
                        str(getattr(current_guild, "name", "سيرفر")).replace(
                            "\n", " "
                        )
                    )[:200]
                    checked_count += 1
                    try:
                        invite = await _find_permanent_invite(
                            current_guild,
                            require_inspection=True,
                        )
                    except InviteInspectionError as error:
                        remaining_inspection_failure_count += 1
                        if _is_temporary_invite_inspection_error(error):
                            still_retryable[retry_guild_id] = current_guild
                            retry_cooldown_until[retry_guild_id] = (
                                time.monotonic() + INVITE_RETRY_COOLDOWN_SECONDS
                            )
                        else:
                            retry_cooldown_until.pop(retry_guild_id, None)
                        retry_report_lines.append(
                            f"• {name} | ID: {retry_guild_id} | "
                            f"{_invite_inspection_failure_reason(error)} "
                            "لم يُنشأ رابط جديد."
                        )
                    else:
                        recovered_inspection_count += 1
                        retry_cooldown_until.pop(retry_guild_id, None)
                        if invite is None:
                            retry_report_lines.append(
                                f"• {name} | ID: {retry_guild_id} | "
                                "اكتمل الفحص؛ لا توجد دعوة دائمة قابلة لإعادة الاستخدام. "
                                "لم يُنشأ رابط جديد."
                            )
                        else:
                            retry_report_lines.append(
                                f"• {name} | ID: {retry_guild_id} | "
                                f"اكتمل الفحص؛ الدعوة الدائمة الحالية: {invite.url}"
                            )

            cooldown_summary = "، ".join(
                f"ID {guild_id}: {seconds} ثانية"
                for guild_id, seconds in cooldown_wait_seconds.items()
            )
            cooldown_notice = (
                f" ما زالت هذه السيرفرات ضمن مهلة الانتظار: {cooldown_summary}."
                if cooldown_wait_seconds
                else ""
            )

            if selected_guild_id is None:
                replace_retryable_inspection_guilds(still_retryable)
            else:
                retryable_inspection_guilds.pop(selected_guild_id, None)
                retryable_inspection_guilds.update(still_retryable)

            if (
                checked_count == 0
                and len(cooldown_wait_seconds) == len(retry_candidates)
            ):
                await reply(
                    ctx,
                    "لم يُعَد فحص أي سيرفر لأنها ضمن مهلة الانتظار. "
                    f"أعد المحاولة بعد: {cooldown_summary}. "
                    "لم يُرسل أي طلب إلى Discord.",
                )
                return

            if len(retry_candidates) > 1:
                retry_report_lines = [
                    "ملخص نتائج إعادة الفحص:",
                    (
                        "• اكتمل فحص الدعوات بنجاح: "
                        f"**{recovered_inspection_count}**"
                    ),
                    (
                        "• لا تزال أخطاء فحص الدعوات موجودة: "
                        f"**{remaining_inspection_failure_count}**"
                    ),
                    f"• لم يُعَد فحصها: **{not_rechecked_count}**",
                    "",
                    "التفاصيل لكل سيرفر:",
                    *retry_report_lines,
                ]

            try:
                for message in _developer_invite_report_chunks(
                    retry_report_lines,
                    retry_temporary=True,
                ):
                    await ctx.author.send(
                        message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except discord.Forbidden:
                await reply(
                    ctx,
                    "تعذر إرسال نتيجة إعادة الفحص إلى الخاص. افتح استقبال الرسائل "
                    "الخاصة من البوت ثم أعد المحاولة." + cooldown_notice,
                )
                return
            except discord.HTTPException:
                logger.exception("Could not deliver the private invite retry report.")
                await reply(
                    ctx,
                    "تعذر إرسال نتيجة إعادة الفحص إلى الخاص بسبب خطأ من Discord."
                    + cooldown_notice,
                )
                return

            await reply(
                ctx,
                f"أعدت فحص {checked_count} سيرفر؛ "
                "لم تُنشأ أو تُغيّر أي دعوة."
                + cooldown_notice,
            )
            return

        try:
            selected_guild_ids, create_missing, needs_attention = (
                _parse_developer_invite_report_options(options)
            )
        except ValueError:
            await reply(
                ctx,
                "الاستخدام: `!رابط_سيرفر [ID ...] [مراجعة] [إنشاء]` أو "
                "`!رابط_سيرفر إعادة_فحص [ID]` أو `!رابط_سيرفر استعادة` أو "
                "`!رابط_سيرفر حذف_التقارير`. "
                "يمكن تحديد عدة سيرفرات بكتابة معرّفاتها مفصولة بمسافات أو فواصل. "
                "أضف `مراجعة` لإظهار السيرفرات التي لا تملك دعوة دائمة قابلة "
                "لإعادة الاستخدام أو تعذر فحص دعواتها. لا تجمع `مراجعة` مع `إنشاء`. "
                "تُعيد `إعادة_فحص` فحص أخطاء Discord المؤقتة فقط، دون تغيير الروابط. "
                "يحذف `حذف_التقارير` نسخ الاستعادة المحفوظة نهائيًا دون إرسالها. "
                "بدون `إنشاء` لا تُنشأ روابط جديدة.",
            )
            return

        if selected_guild_ids is None:
            guilds = list(bot.guilds)
        else:
            guilds = [
                bot.get_guild(selected_guild_id)
                for selected_guild_id in selected_guild_ids
            ]
            missing_guild_ids = [
                selected_guild_id
                for selected_guild_id, guild in zip(selected_guild_ids, guilds)
                if guild is None
            ]
            if missing_guild_ids:
                missing_ids = ", ".join(str(guild_id) for guild_id in missing_guild_ids)
                await reply(
                    ctx,
                    "لم أجد هذه السيرفرات ضمن السيرفرات المتصلة بالبوت: "
                    f"`{missing_ids}`. لم تُفحص أي دعوة.",
                )
                return

        if create_missing:
            if selected_guild_ids is None:
                scope = f"جميع السيرفرات التي يتواجد فيها البوت ({len(guilds)})"
            else:
                scope = f"السيرفرات المحددة ({len(guilds)})"
            result = await send_invite_batch_preview(
                ctx.author,
                guilds,
                scope_label=scope,
                over_limit_hint=(
                    "قلل النطاق باستخدام `!رابط_سيرفر [ID ...] إنشاء` "
                    "ثم أنشئ معاينة جديدة."
                ),
            )
            await reply(ctx, result)
            return

        report_lines = []
        report_retryable = {}
        reusable_count = 0
        missing_count = 0
        inspection_failure_count = 0
        permission_failure_count = 0
        temporary_failure_count = 0
        other_inspection_failure_count = 0
        if not guilds:
            report_lines.append("البوت غير موجود حاليًا في أي سيرفر.")
        for guild in guilds:
            name = discord.utils.escape_markdown(
                str(getattr(guild, "name", "سيرفر")).replace("\n", " ")
            )[:200]
            try:
                invite = await _find_permanent_invite(
                    guild,
                    require_inspection=True,
                )
            except InviteInspectionError as error:
                if _is_temporary_invite_inspection_error(error):
                    report_retryable[guild.id] = guild
                inspection_failure_count += 1
                cause = error.__cause__
                if isinstance(cause, discord.Forbidden):
                    permission_failure_count += 1
                elif isinstance(cause, discord.HTTPException):
                    temporary_failure_count += 1
                else:
                    other_inspection_failure_count += 1
                report_lines.append(
                    f"• {name} | ID: {guild.id} | "
                    f"الحالة: {_invite_inspection_failure_reason(error)} "
                    "لم يُنشأ رابط جديد."
                )
            except NoInviteChannelError:
                report_lines.append(
                    f"• {name} | ID: {guild.id} | تعذر إنشاء دعوة."
                )
            except discord.Forbidden:
                report_lines.append(
                    f"• {name} | ID: {guild.id} | لا يملك البوت صلاحية إنشاء دعوة."
                )
            except discord.HTTPException:
                logger.exception("Could not create an invite for guild %s.", guild.id)
                report_lines.append(
                    f"• {name} | ID: {guild.id} | تعذر إنشاء دعوة بسبب خطأ من Discord."
                )
            else:
                if invite is None:
                    missing_count += 1
                    report_lines.append(
                        f"• {name} | ID: {guild.id} | "
                        "الحالة: لا توجد دعوة دائمة قابلة لإعادة الاستخدام؛ "
                        "لم يُنشأ رابط جديد."
                    )
                elif not needs_attention:
                    reusable_count += 1
                    report_lines.append(f"• {name} | ID: {guild.id} | {invite.url}")

        replace_retryable_inspection_guilds(report_retryable)

        if needs_attention and guilds and not report_lines:
            report_lines.append("لا توجد سيرفرات تحتاج إلى مراجعة.")

        summary_counts = (
            reusable_count,
            missing_count,
            inspection_failure_count,
            permission_failure_count,
            temporary_failure_count,
            other_inspection_failure_count,
        ) if len(guilds) > 1 else None
        try:
            for message in _developer_invite_report_chunks(
                report_lines,
                create_missing=create_missing,
                needs_attention=needs_attention,
                summary_counts=summary_counts,
            ):
                await ctx.author.send(
                    message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            await reply(
                ctx,
                "تعذر إرسال التفاصيل إلى الخاص. افتح استقبال الرسائل الخاصة من البوت ثم أعد المحاولة.",
            )
            return
        except discord.HTTPException:
            logger.exception("Could not deliver the private developer invite report.")
            await reply(
                ctx,
                "تعذر إرسال التفاصيل إلى الخاص بسبب خطأ من Discord. حاول لاحقًا.",
            )
            return

        await reply(ctx, "أرسلت التفاصيل إلى الخاص.")

    @bot.command(
        name="إلغاء_دعوات_البوت",
        aliases=["revoke_bot_invites"],
    )
    async def revoke_bot_invites(ctx):
        """Privately list bot-created invites and confirm before revoking one."""
        if ctx.author.id != developer_id:
            await reply(ctx, "هذا الأمر خاص بمطور البوت فقط.")
            return

        bot_id = getattr(getattr(bot, "user", None), "id", None)
        if bot_id is None:
            await reply(ctx, "تعذر التحقق من هوية البوت حاليًا.")
            return

        entries = []
        scan_errors = []
        for guild in bot.guilds:
            try:
                invites = await guild.invites()
            except discord.Forbidden:
                scan_errors.append(
                    f"{discord.utils.escape_markdown(guild.name)[:55]} (`{guild.id}`)"
                )
                continue
            except discord.HTTPException:
                logger.exception(
                    "Could not list invites in guild %s for developer revocation.",
                    guild.id,
                )
                scan_errors.append(
                    f"{discord.utils.escape_markdown(guild.name)[:55]} (`{guild.id}`)"
                )
                continue

            for invite in invites:
                if (
                    getattr(
                        getattr(invite, "inviter", None),
                        "id",
                        None,
                    )
                    == bot_id
                    and not getattr(invite, "revoked", False)
                    and getattr(invite, "code", None)
                ):
                    entries.append((guild, invite))

        if not entries:
            message = "لا توجد دعوات نشطة أنشأها البوت."
            if scan_errors:
                message += (
                    "\nتعذر فحص بعض السيرفرات؛ قد توجد دعوات لم أتمكن من عرضها."
                )
            view = None
        else:
            view = BotInviteListView(entries, scan_errors)
            message = render_bot_invite_page(entries, 0, scan_errors)

        try:
            private_message = await ctx.author.send(
                message,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if view is not None:
                view.message = private_message
        except discord.Forbidden:
            await reply(
                ctx,
                "تعذر إرسال قائمة الدعوات إلى الخاص. افتح استقبال الرسائل الخاصة من البوت ثم أعد المحاولة.",
            )
            return
        except discord.HTTPException:
            logger.exception("Could not send the private bot invite list.")
            await reply(ctx, "تعذر إرسال قائمة الدعوات إلى الخاص بسبب خطأ من Discord.")
            return

        await reply(ctx, "أرسلت قائمة الدعوات إلى الخاص.")

    @bot.command(name="احصائيات_دعوات", aliases=["invite_stats"])
    async def invite_stats(ctx):
        """Show invite usage for the current server."""
        guild = ctx.guild
        if guild is None:
            await reply(ctx, "هذا الأمر يعمل داخل السيرفرات فقط.")
            return

        try:
            invites = await guild.invites()
        except discord.Forbidden:
            await reply(
                ctx,
                "لا أستطيع قراءة الدعوات. يحتاج البوت صلاحية **Manage Server** "
                "لإظهار الإحصاءات.",
            )
            return
        except discord.HTTPException:
            logger.exception("Could not read invite statistics for guild %s.", guild.id)
            await reply(ctx, "تعذر جلب إحصاءات الدعوات من Discord.")
            return

        await ctx.send(
            embed=_invites_embed(guild, invites),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="احذف_دعواتي", aliases=["delete_my_invites"])
    async def delete_my_invites(ctx):
        """Delete only the current member's invites in the current server."""
        guild = ctx.guild
        if guild is None:
            await reply(ctx, "هذا الأمر يعمل داخل السيرفرات فقط.")
            return

        try:
            invites = await guild.invites()
        except discord.Forbidden:
            await reply(
                ctx,
                "لا أستطيع قراءة الدعوات لحذفها. يحتاج البوت صلاحية **Manage Server**.",
            )
            return
        except discord.HTTPException:
            logger.exception("Could not list invites for deletion in guild %s.", guild.id)
            await reply(ctx, "تعذر جلب دعواتك من Discord.")
            return

        owned_invites = [
            invite
            for invite in invites
            if getattr(getattr(invite, "inviter", None), "id", None)
            == ctx.author.id
        ]
        if not owned_invites:
            await reply(ctx, "لا توجد دعوات أنشأتها في هذا السيرفر.")
            return

        deleted = 0
        failed = 0
        for invite in owned_invites:
            try:
                await invite.delete(
                    reason=f"Invite deletion requested by member {ctx.author.id}"
                )
                deleted += 1
            except discord.NotFound:
                deleted += 1
            except discord.HTTPException:
                logger.exception(
                    "Could not delete invite %s in guild %s.",
                    getattr(invite, "code", "unknown"),
                    guild.id,
                )
                failed += 1

        result = f"حُذفت **{deleted}** من دعواتك."
        if failed:
            result += f" وتعذر حذف **{failed}**؛ تحقق من صلاحية Manage Server."
        await reply(ctx, result)

    registered = {
        "server_invite": bot.get_command("رابط"),
        "developer_server_invite": bot.get_command("رابط_سيرفر"),
        "revoke_bot_invites": bot.get_command("إلغاء_دعوات_البوت"),
        "invite_stats": bot.get_command("احصائيات_دعوات"),
        "delete_my_invites": bot.get_command("احذف_دعواتي"),
    }
    bot._invite_commands = registered
    return registered