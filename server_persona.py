"""Per-server names and avatars for Discord chat replies."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import discord
from discord import app_commands


logger = logging.getLogger(__name__)

MAX_DISPLAY_NAME_LENGTH = 80
MAX_AVATAR_BYTES = 8 * 1024 * 1024


class PersonaSettingsError(ValueError):
    """A safe, user-facing validation error for persona settings."""


def _guild_key(guild_id) -> str:
    value = str(guild_id)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise PersonaSettingsError("⚠️ معرّف السيرفر غير صالح.")
    return value


def _validate_avatar_url(value: str) -> str:
    url = value.strip()
    if len(url) > 2048:
        raise PersonaSettingsError("⚠️ رابط الصورة أطول من الحد المسموح.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        hostname = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or any(character.isspace() for character in url)
    ):
        raise PersonaSettingsError("⚠️ أرسل رابط صورة عامًّا يبدأ بـ https://.")
    return url


def _image_extension(image: bytes) -> str:
    if len(image) > MAX_AVATAR_BYTES:
        raise PersonaSettingsError("⚠️ حجم الصورة أكبر من 8 ميغابايت.")
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    raise PersonaSettingsError(
        "⚠️ استخدم صورة PNG أو JPG أو GIF، وبحجم لا يتجاوز 8 ميغابايت."
    )


class ServerPersonaStore:
    """Store per-guild overrides atomically; the guild icon/name are defaults."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.avatar_dir = self.path.parent / "server_persona_avatars"
        self.backup_dir = self.path.parent / "server_persona_backups"
        self._settings = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"Invalid server persona settings at {self.path}"
            ) from error
        if not isinstance(data, dict):
            raise ValueError("Server persona settings must be a JSON object.")

        for guild_id, entry in data.items():
            if (
                not guild_id.isascii()
                or not guild_id.isdecimal()
                or int(guild_id) <= 0
            ):
                raise ValueError("Server persona settings contain an invalid guild ID.")
            if not isinstance(entry, dict) or not entry:
                raise ValueError("Server persona settings contain an invalid entry.")
            if set(entry) - {
                "display_name",
                "avatar_url",
                "avatar_file",
                "use_webhook",
                "webhook_only_images",
            }:
                raise ValueError("Server persona settings contain an unknown field.")
            if "use_webhook" in entry and type(entry["use_webhook"]) is not bool:
                raise ValueError("Server persona settings contain an invalid mode.")
            if (
                "webhook_only_images" in entry
                and type(entry["webhook_only_images"]) is not bool
            ):
                raise ValueError("Server persona settings contain an invalid mode.")
            if entry.get("use_webhook") is False and entry.get("webhook_only_images"):
                raise ValueError("Server persona settings contain conflicting modes.")

            display_name = entry.get("display_name")
            if "display_name" in entry and (
                not isinstance(display_name, str)
                or not display_name.strip()
                or len(display_name) > MAX_DISPLAY_NAME_LENGTH
                or any(ord(character) < 32 for character in display_name)
            ):
                raise ValueError("Server persona settings contain an invalid name.")

            avatar_url = entry.get("avatar_url")
            avatar_file = entry.get("avatar_file")
            if "avatar_url" in entry and not isinstance(avatar_url, str):
                raise ValueError("Server persona settings contain an invalid URL.")
            if "avatar_file" in entry and not isinstance(avatar_file, str):
                raise ValueError("Server persona settings contain an invalid image path.")
            if avatar_url is not None and avatar_file is not None:
                raise ValueError("A persona cannot have both an avatar URL and file.")
            if avatar_url is not None:
                _validate_avatar_url(avatar_url)
            if avatar_file is not None and not re.fullmatch(
                rf"{guild_id}/[0-9a-f]{{64}}\.(?:png|jpg|gif)",
                avatar_file,
            ):
                raise ValueError("Server persona settings contain an invalid image path.")
            if not any(
                key in entry
                for key in (
                    "display_name",
                    "avatar_url",
                    "avatar_file",
                    "use_webhook",
                    "webhook_only_images",
                )
            ):
                raise ValueError("Server persona settings contain an empty entry.")
        return data

    @staticmethod
    def _write_json_atomic(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _save(self, updated: dict[str, dict]) -> None:
        self._write_json_atomic(self.path, updated)
        self._settings = updated

    def get(self, guild_id) -> dict:
        return dict(self._settings.get(_guild_key(guild_id), {}))

    def set_webhook_enabled(self, guild_id, enabled: bool) -> None:
        self.set_webhook_mode(guild_id, "all" if enabled else "off")

    def set_webhook_mode(self, guild_id, mode: str) -> None:
        key = _guild_key(guild_id)
        if mode not in {"all", "off", "images"}:
            raise PersonaSettingsError("⚠️ وضع الرد غير صالح.")

        entry = self.get(key)
        if mode == "all":
            entry.pop("use_webhook", None)
            entry.pop("webhook_only_images", None)
        elif mode == "off":
            entry["use_webhook"] = False
            entry.pop("webhook_only_images", None)
        else:
            entry.pop("use_webhook", None)
            entry["webhook_only_images"] = True

        updated = dict(self._settings)
        if entry:
            updated[key] = entry
        else:
            updated.pop(key, None)
        self._save(updated)

    def set_display_name(self, guild_id, value: str) -> str:
        key = _guild_key(guild_id)
        name = value.strip()
        if (
            not name
            or len(name) > MAX_DISPLAY_NAME_LENGTH
            or any(ord(character) < 32 for character in name)
        ):
            raise PersonaSettingsError(
                f"⚠️ اكتب اسمًا من 1 إلى {MAX_DISPLAY_NAME_LENGTH} حرفًا."
            )
        updated = {**self._settings, key: {**self.get(key), "display_name": name}}
        self._save(updated)
        return name

    def set_avatar_url(self, guild_id, value: str) -> str:
        key = _guild_key(guild_id)
        url = _validate_avatar_url(value)
        entry = self.get(key)
        entry.pop("avatar_file", None)
        entry["avatar_url"] = url
        self._save({**self._settings, key: entry})
        return url

    def set_avatar_bytes(self, guild_id, image: bytes) -> str:
        key = _guild_key(guild_id)
        extension = _image_extension(image)
        digest = hashlib.sha256(image).hexdigest()
        relative_path = f"{key}/{digest}.{extension}"
        destination = self.avatar_dir / relative_path

        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".avatar-",
                suffix=".tmp",
                dir=destination.parent,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(image)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, destination)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise

        entry = self.get(key)
        entry.pop("avatar_url", None)
        entry["avatar_file"] = relative_path
        try:
            self._save({**self._settings, key: entry})
        except OSError:
            raise
        return relative_path

    def read_avatar(self, guild_id) -> bytes | None:
        entry = self.get(guild_id)
        relative_path = entry.get("avatar_file")
        if not relative_path:
            return None
        try:
            return (self.avatar_dir / relative_path).read_bytes()
        except FileNotFoundError:
            return None

    def reset_guild(self, guild_id) -> bool:
        key = _guild_key(guild_id)
        current = self._settings.get(key)
        if current is None:
            return False

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.backup_dir / f"{key}_{stamp}_{uuid.uuid4().hex}.json"
        backup = {
            "guild_id": key,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "settings": current,
        }
        self._write_json_atomic(backup_path, backup)

        updated = dict(self._settings)
        del updated[key]
        self._save(updated)
        return True


class ResetPersonaView(discord.ui.View):
    def __init__(
        self,
        manager: "ServerPersonaManager",
        guild_id: int,
        requester_id: int,
    ):
        super().__init__(timeout=60)
        self.manager = manager
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.message = None

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "🔒 هذه التأكيدات مرتبطة بالشخص الذي بدأ الأمر.",
                ephemeral=True,
            )
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.administrator:
            await interaction.response.edit_message(
                content="⚠️ يلزم أن تكون إداريًا لتأكيد إعادة الضبط.",
                view=None,
            )
            return False
        return True

    @discord.ui.button(
        label="تأكيد إعادة الضبط",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._authorize(interaction):
            return
        try:
            reset = self.manager.store.reset_guild(self.guild_id)
        except (OSError, ValueError):
            logger.exception("Could not back up/reset persona for guild %s", self.guild_id)
            await interaction.response.edit_message(
                content="❌ تعذر حفظ النسخة الاحتياطية؛ لم تتغير الإعدادات.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=(
                "✅ أُعيدت الإعدادات إلى أيقونة واسم السيرفر الافتراضيين."
                if reset
                else "ℹ️ الإعدادات بالفعل على الوضع الافتراضي."
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(
        label="إلغاء",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._authorize(interaction):
            return
        await interaction.response.edit_message(
            content="↩️ أُلغيت إعادة الضبط؛ لم تتغير الإعدادات.",
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="⏰ انتهت مهلة التأكيد؛ لم تتغير الإعدادات.",
                    view=self,
                )
            except discord.HTTPException:
                logger.debug("Could not disable an expired persona reset view.")


class ServerPersonaManager:
    def __init__(self, bot, store: ServerPersonaStore):
        self.bot = bot
        self.store = store
        self._webhooks: dict[int, discord.Webhook] = {}
        self._avatar_hashes: dict[int, str] = {}
        self._warned_channels: set[int] = set()
        self._channel_locks: dict[int, asyncio.Lock] = {}

    def _default_persona(self, guild: discord.Guild) -> tuple[str, str | None]:
        user = self.bot.user
        name = guild.name[:MAX_DISPLAY_NAME_LENGTH]
        icon = guild.icon
        if icon is not None:
            avatar_url = icon.url
        elif user is not None:
            avatar_url = user.display_avatar.url
        else:
            avatar_url = None
        return name, avatar_url

    def effective_persona(self, guild: discord.Guild) -> tuple[str, str | None]:
        settings = self.store.get(guild.id)
        default_name, default_avatar = self._default_persona(guild)
        name = settings.get("display_name") or default_name
        avatar_url = settings.get("avatar_url") or default_avatar
        return discord.utils.escape_mentions(name), avatar_url

    @staticmethod
    def _target_channel(message: discord.Message):
        if isinstance(message.channel, discord.Thread):
            return message.channel.parent, message.channel
        return message.channel, None

    def _webhook_name(self) -> str:
        user_id = self.bot.user.id if self.bot.user is not None else "pending"
        return f"server-persona-{user_id}"

    async def _get_or_create_webhook(self, channel):
        if channel is None or not hasattr(channel, "webhooks"):
            return None
        cached = self._webhooks.get(channel.id)
        if cached is not None:
            return cached

        guild = getattr(channel, "guild", None)
        bot_member = getattr(guild, "me", None)
        if bot_member is None or not channel.permissions_for(bot_member).manage_webhooks:
            if channel.id not in self._warned_channels:
                logger.warning(
                    "Custom persona needs Manage Webhooks in channel %s; "
                    "regular bot replies will be used.",
                    channel.id,
                )
                self._warned_channels.add(channel.id)
            return None

        try:
            webhook_name = self._webhook_name()
            webhooks = await channel.webhooks()
            webhook = next(
                (item for item in webhooks if item.name == webhook_name),
                None,
            )
            if webhook is None:
                webhook = await channel.create_webhook(
                    name=webhook_name,
                    reason="Per-server bot reply name and avatar",
                )
            self._webhooks[channel.id] = webhook
            return webhook
        except discord.Forbidden:
            logger.warning(
                "Discord denied webhook access in channel %s; using regular reply.",
                channel.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Could not prepare a persona webhook in channel %s.",
                channel.id,
            )
        return None

    async def _regular_reply(
        self,
        message: discord.Message,
        content: str,
        *,
        mention_author: bool = False,
    ) -> None:
        allowed_mentions = (
            discord.AllowedMentions(
                everyone=False,
                users=[message.author],
                roles=False,
                replied_user=True,
            )
            if mention_author
            else discord.AllowedMentions.none()
        )
        await message.reply(
            content,
            mention_author=mention_author,
            allowed_mentions=allowed_mentions,
        )

    async def reply(
        self,
        message: discord.Message,
        content: str,
        *,
        force_webhook: bool = False,
    ) -> None:
        guild = message.guild
        if guild is None:
            await self._regular_reply(message, content)
            return

        settings = self.store.get(guild.id)
        if not settings.get("use_webhook", True) or (
            settings.get("webhook_only_images", False) and not force_webhook
        ):
            await self._regular_reply(message, content, mention_author=True)
            return

        username, default_avatar_url = self._default_persona(guild)
        username = settings.get("display_name") or username
        username = discord.utils.escape_mentions(username)
        avatar_url = settings.get("avatar_url") or default_avatar_url

        avatar_data = None
        if settings.get("avatar_file"):
            try:
                avatar_data = self.store.read_avatar(guild.id)
            except OSError:
                logger.exception("Could not read persona image for guild %s", guild.id)
            if avatar_data is None:
                logger.warning(
                    "Saved persona image is unavailable for guild %s; "
                    "using the server's default avatar.",
                    guild.id,
                )
                avatar_url = default_avatar_url

        target_channel, thread = self._target_channel(message)
        if target_channel is None:
            await self._regular_reply(message, content, mention_author=True)
            return

        lock = self._channel_locks.setdefault(
            target_channel.id,
            asyncio.Lock(),
        )
        async with lock:
            webhook = await self._get_or_create_webhook(target_channel)
            if webhook is None:
                await self._regular_reply(message, content, mention_author=True)
                return

            for attempt in range(2):
                try:
                    if avatar_data is not None:
                        digest = hashlib.sha256(avatar_data).hexdigest()
                        if self._avatar_hashes.get(webhook.id) != digest:
                            webhook = await webhook.edit(
                                avatar=avatar_data,
                                reason="Apply the configured server persona avatar",
                            )
                            self._webhooks[target_channel.id] = webhook
                            self._avatar_hashes[webhook.id] = digest

                    kwargs = {
                        "username": username,
                        "allowed_mentions": discord.AllowedMentions(
                            everyone=False,
                            users=[message.author],
                            roles=False,
                            replied_user=False,
                        ),
                        "wait": True,
                    }
                    if avatar_data is None and avatar_url:
                        kwargs["avatar_url"] = avatar_url
                    if thread is not None:
                        kwargs["thread"] = thread
                    mention = message.author.mention
                    webhook_content = f"{mention} {content}"
                    max_content_length = 2000 - len(mention) - 1
                    if len(content) > max_content_length:
                        clipped_length = max_content_length - 1
                        content = content[:clipped_length].rstrip() + "…"
                        webhook_content = f"{mention} {content}"
                    await webhook.send(content=webhook_content, **kwargs)
                    return
                except discord.NotFound:
                    self._webhooks.pop(target_channel.id, None)
                    self._avatar_hashes.pop(webhook.id, None)
                    if attempt == 0:
                        webhook = await self._get_or_create_webhook(target_channel)
                        if webhook is not None:
                            continue
                    logger.warning(
                        "Persona webhook is unavailable in channel %s; "
                        "using regular reply.",
                        target_channel.id,
                    )
                    break
                except discord.Forbidden:
                    logger.warning(
                        "Discord denied persona webhook sending in channel %s; "
                        "using regular reply.",
                        target_channel.id,
                    )
                    break
                except discord.HTTPException:
                    logger.exception(
                        "Persona webhook send failed in channel %s; "
                        "using regular reply.",
                        target_channel.id,
                    )
                    break

        await self._regular_reply(message, content, mention_author=True)

    def _summary(self, guild: discord.Guild) -> tuple[discord.Embed, list[discord.File]]:
        """يبني embed أنيق لملخص الهوية."""
        settings = self.store.get(guild.id)
        name, avatar_url = self.effective_persona(guild)
        
        embed = discord.Embed(
            title="🎭  هوية ردود البوت",
            description=f"### في سيرفر **{guild.name}**\n\u200b",
            color=0x4A90E2,
            timestamp=datetime.now(timezone.utc),
        )
        
        # ═══ وضع الرد ═══
        if not settings.get("use_webhook", True):
            mode_description = "🤖 **حساب البوت** — الردود باسم وصورة البوت"
        elif settings.get("webhook_only_images", False):
            mode_description = "🖼️ **صور فقط** — Webhook للصور، وحساب البوت للنصوص"
        else:
            mode_description = "✅ **هوية السيرفر** — الردود باسم وصورة السيرفر"
        
        embed.add_field(name="📊 وضع الرد", value=mode_description, inline=False)
        embed.add_field(name="🏷️ الاسم", value=f"**{name}**", inline=False)
        
        # ═══ الصورة ═══
        files: list[discord.File] = []
        avatar_file = settings.get("avatar_file")
        if avatar_file:
            try:
                image = self.store.read_avatar(guild.id)
            except OSError:
                logger.exception("Could not read persona image for guild %s", guild.id)
                image = None
            if image is not None:
                filename = Path(avatar_file).name
                files.append(discord.File(io.BytesIO(image), filename=filename))
                embed.set_thumbnail(url=f"attachment://{filename}")
                avatar_description = "🖼️ صورة مرفوعة ومحفوظة"
            else:
                avatar_description = "⚠️ الصورة المرفوعة غير متاحة"
        elif settings.get("avatar_url"):
            avatar_description = "🔗 رابط صورة مخصص"
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
        elif guild.icon is not None:
            avatar_description = "🏰 أيقونة السيرفر (افتراضي)"
            embed.set_thumbnail(url=guild.icon.url)
        else:
            avatar_description = "🤖 صورة حساب البوت (افتراضي)"
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
        
        embed.add_field(name="🖼️ الصورة", value=avatar_description, inline=False)
        embed.set_footer(text="💡 استخدم /set أو /setting لتغيير الهوية")
        return embed, files

    def setup_commands(self) -> None:
        existing = getattr(self.bot, "_server_persona_command", None)
        if existing is not None:
            return

        @self.bot.tree.command(
            name="set",
            description="تخصيص اسم وصورة ردود البوت في هذا السيرفر",
        )
        @app_commands.guild_only()
        @app_commands.describe(
            action="اختر الإعداد الذي تريد تغييره",
            value="اسم جديد أو رابط HTTPS لصورة",
            image="ارفع صورة PNG أو JPG أو GIF",
        )
        @app_commands.choices(
            action=[
                app_commands.Choice(name="🖼️ تغيير الصورة", value="image"),
                app_commands.Choice(name="🏷️ تغيير الاسم", value="name"),
                app_commands.Choice(name="👁️ عرض الإعدادات", value="show"),
                app_commands.Choice(name="✅ تفعيل هوية السيرفر", value="auto_on"),
                app_commands.Choice(name="🤖 استخدام حساب البوت", value="auto_off"),
                app_commands.Choice(name="📷 Webhook للصور فقط", value="image_only"),
                app_commands.Choice(name="🔄 إعادة الافتراضي", value="reset"),
            ]
        )
        async def set_command(
            interaction: discord.Interaction,
            action: app_commands.Choice[str],
            value: str | None = None,
            image: discord.Attachment | None = None,
        ) -> None:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    "⚠️ هذا الأمر متاح داخل السيرفر فقط.",
                    ephemeral=True,
                )
                return
            permissions = getattr(interaction.user, "guild_permissions", None)
            if permissions is None or not permissions.administrator:
                await interaction.response.send_message(
                    "🔒 تغيير هوية الردود متاح لإداريي السيرفر فقط.",
                    ephemeral=True,
                )
                return

            if action.value in ("auto_on", "auto_off", "image_only"):
                mode = {
                    "auto_on": "all",
                    "auto_off": "off",
                    "image_only": "images",
                }[action.value]
                try:
                    self.store.set_webhook_mode(guild.id, mode)
                except (OSError, PersonaSettingsError):
                    logger.exception(
                        "Could not change persona mode for guild %s",
                        guild.id,
                    )
                    await interaction.response.send_message(
                        "❌ تعذر حفظ وضع الرد؛ لم تتغير الإعدادات.",
                        ephemeral=True,
                    )
                    return
                embed, files = self._summary(guild)
                await interaction.response.send_message(
                    content=(
                        "✅ فُعّلت هوية السيرفر."
                        if mode == "all"
                        else (
                            "🤖 أُوقفت هوية السيرفر. الردود ستظهر باسم البوت."
                            if mode == "off"
                            else "📷 فُعّل Webhook لردود الصور فقط."
                        )
                    ),
                    embed=embed,
                    files=files,
                    ephemeral=True,
                )
                return

            if action.value == "show":
                embed, files = self._summary(guild)
                await interaction.response.send_message(
                    embed=embed,
                    files=files,
                    ephemeral=True,
                )
                return

            if action.value == "reset":
                view = ResetPersonaView(self, guild.id, interaction.user.id)
                await interaction.response.send_message(
                    "⚠️ ستُحذف التخصيصات لهذا السيرفر وتُحفظ نسخة احتياطية.",
                    view=view,
                    ephemeral=True,
                )
                view.message = await interaction.original_response()
                return

            if action.value == "name":
                if not value:
                    await interaction.response.send_message(
                        "⚠️ اكتب الاسم الجديد في خانة `value`.",
                        ephemeral=True,
                    )
                    return
                try:
                    self.store.set_display_name(guild.id, value)
                except PersonaSettingsError as error:
                    await interaction.response.send_message(str(error), ephemeral=True)
                    return
                except OSError:
                    logger.exception("Could not save persona name for guild %s", guild.id)
                    await interaction.response.send_message(
                        "❌ تعذر حفظ الاسم؛ لم تتغير الإعدادات.",
                        ephemeral=True,
                    )
                    return
                embed, files = self._summary(guild)
                await interaction.response.send_message(
                    content="✅ حُفظ الاسم.",
                    embed=embed,
                    files=files,
                    ephemeral=True,
                )
                return

            if action.value == "image":
                if image is not None and value:
                    await interaction.response.send_message(
                        "⚠️ ارفع صورة أو أدخل رابطًا، ولا تستخدم الاثنين معًا.",
                        ephemeral=True,
                    )
                    return
                if image is None and not value:
                    await interaction.response.send_message(
                        "⚠️ ارفع صورة PNG/JPG/GIF، أو أدخل رابط HTTPS.",
                        ephemeral=True,
                    )
                    return
                if image is not None and image.size > MAX_AVATAR_BYTES:
                    await interaction.response.send_message(
                        "⚠️ حجم الصورة أكبر من 8 ميغابايت.",
                        ephemeral=True,
                    )
                    return

                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    if image is not None:
                        image_bytes = await image.read()
                        self.store.set_avatar_bytes(guild.id, image_bytes)
                    elif value:
                        self.store.set_avatar_url(guild.id, value)
                except PersonaSettingsError as error:
                    await interaction.edit_original_response(
                        content=str(error),
                        embed=None,
                        attachments=[],
                    )
                    return
                except (OSError, discord.HTTPException):
                    logger.exception("Could not save persona image for guild %s", guild.id)
                    await interaction.edit_original_response(
                        content="❌ تعذر حفظ الصورة؛ لم تتغير الإعدادات.",
                        embed=None,
                        attachments=[],
                    )
                    return
                embed, files = self._summary(guild)
                await interaction.edit_original_response(
                    content="✅ حُفظت الصورة.",
                    embed=embed,
                    attachments=files,
                )
                return

            await interaction.response.send_message(
                "❌ الإجراء غير معروف.",
                ephemeral=True,
            )

        @self.bot.tree.command(
            name="setting",
            description="إعدادات هوية وردود البوت في هذا السيرفر",
        )
        @app_commands.guild_only()
        @app_commands.describe(
            action="اختر الإعداد الذي تريد تغييره",
            value="اسم جديد أو رابط HTTPS لصورة",
            image="ارفع صورة PNG أو JPG أو GIF",
        )
        @app_commands.choices(
            action=[
                app_commands.Choice(name="🖼️ صورة مخصصة", value="image"),
                app_commands.Choice(name="🏷️ اسم مخصص", value="name"),
                app_commands.Choice(name="👁️ عرض الإعدادات", value="show"),
                app_commands.Choice(name="✅ تفعيل هوية السيرفر", value="auto_on"),
                app_commands.Choice(name="🤖 استخدام حساب البوت", value="auto_off"),
                app_commands.Choice(name="📷 Webhook للصور فقط", value="image_only"),
                app_commands.Choice(name="🔄 إعادة الافتراضي", value="reset"),
            ]
        )
        async def setting_command(
            interaction: discord.Interaction,
            action: app_commands.Choice[str],
            value: str | None = None,
            image: discord.Attachment | None = None,
        ) -> None:
            await set_command.callback(interaction, action, value, image)

        self.bot._server_persona_command = set_command
        self.bot._server_persona_setting_command = setting_command