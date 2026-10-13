"""Guild-scoped YouTube music playback, queue, and Discord controls."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import discord
import yt_dlp
from discord.ext import commands


logger = logging.getLogger(__name__)
MAX_QUEUE_SIZE = 50
MAX_QUERY_LENGTH = 200
MAX_TRACK_TITLE_LENGTH = 180
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
    "retries": 1,
    "extractor_retries": 1,
}
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn -loglevel error"


def _ensure_opus_loaded() -> None:
    """Load the system Opus library required by discord.py's voice encoder."""
    if discord.opus.is_loaded():
        return

    try:
        discord.opus.load_opus("libopus.so")
    except OSError as exc:
        raise RuntimeError(
            "Discord music playback requires the libopus system library."
        ) from exc

    logger.info("Loaded Opus voice encoder from libopus.so")


class MusicError(ValueError):
    """An expected, user-correctable music command error."""


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    url: str
    duration: int | None
    requester: str
    thumbnail: str | None = None

    @property
    def display_title(self) -> str:
        safe = discord.utils.escape_markdown(self.title)
        return re.sub(r"[\r\n\t]+", " ", safe)[:MAX_TRACK_TITLE_LENGTH]

    @property
    def duration_text(self) -> str:
        if self.duration is None or self.duration <= 0:
            return "مباشر"
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


def canonical_youtube_url(value: str) -> str:
    """Validate a single YouTube video URL and return its canonical HTTPS URL."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise MusicError("الرابط غير صالح.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise MusicError("استخدم رابط YouTube يبدأ بـ http أو https.")
    try:
        has_credentials_or_port = bool(
            parsed.username or parsed.password or parsed.port
        )
    except ValueError as exc:
        raise MusicError("هذا النوع من روابط YouTube غير مدعوم.") from exc
    if has_credentials_or_port:
        raise MusicError("هذا النوع من روابط YouTube غير مدعوم.")

    host = (parsed.hostname or "").lower().rstrip(".")
    video_id: str | None = None
    query = parse_qs(parsed.query)
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            video_id = query.get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
    else:
        raise MusicError("أرسل رابط فيديو من YouTube فقط.")

    if query.get("list"):
        raise MusicError("روابط قوائم التشغيل غير مدعومة؛ أرسل رابط فيديو واحدًا.")
    if not video_id or not YOUTUBE_ID_PATTERN.fullmatch(video_id):
        raise MusicError("لم أتعرف على فيديو YouTube في هذا الرابط.")
    return f"https://www.youtube.com/watch?v={video_id}"


def _extract_track_sync(query: str, requester: str) -> Track:
    with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
        result = ydl.extract_info(query, download=False)

    if not result:
        raise MusicError("لم أجد نتيجة لهذا الطلب.")
    if result.get("_type") in {"playlist", "multi_video"}:
        entries = result.get("entries") or []
        result = next((entry for entry in entries if entry), None)
    elif result.get("entries") is not None:
        entries = result.get("entries") or []
        result = next((entry for entry in entries if entry), None)
    if not result:
        raise MusicError("لم أجد فيديو مناسبًا.")

    video_id = str(result.get("id") or "")
    if not YOUTUBE_ID_PATTERN.fullmatch(video_id):
        raise MusicError("نتيجة YouTube لا تحتوي على معرّف فيديو صالح.")

    duration = result.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError, OverflowError):
        duration = None

    thumbnail = result.get("thumbnail")
    if not isinstance(thumbnail, str) or not thumbnail.startswith("https://"):
        thumbnail = None

    title = str(result.get("title") or "فيديو بدون عنوان")
    return Track(
        video_id=video_id,
        title=re.sub(r"[\r\n\t]+", " ", title)[:MAX_TRACK_TITLE_LENGTH],
        url=f"https://www.youtube.com/watch?v={video_id}",
        duration=duration,
        requester=requester[:100],
        thumbnail=thumbnail,
    )


def _resolve_audio_url_sync(video_url: str) -> str:
    with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
        info = ydl.extract_info(video_url, download=False)
    audio_url = info.get("url") if info else None
    if not isinstance(audio_url, str) or not audio_url.startswith("https://"):
        raise MusicError("تعذّر تجهيز رابط الصوت من YouTube.")
    return audio_url


@dataclass
class GuildMusicState:
    queue: deque[Track] = field(default_factory=deque)
    history: deque[Track] = field(default_factory=lambda: deque(maxlen=20))
    current: Track | None = None
    loop_mode: str = "off"
    volume: int = 100
    voice_client: object | None = None
    text_channel: object | None = None
    panel_message: object | None = None
    panel_channel_id: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0
    starting: bool = False
    pending_action: str | None = None


class MusicManager:
    """Keep independent, in-memory playback state for each Discord server."""

    def __init__(self, bot: commands.Bot, settings_store=None) -> None:
        _ensure_opus_loaded()
        self.bot = bot
        self.settings_store = settings_store
        self.guilds: dict[int, GuildMusicState] = {}

    def state_for(self, guild_id: int) -> GuildMusicState:
        state = self.guilds.get(guild_id)
        if state is None:
            state = GuildMusicState()
            if self.settings_store is not None:
                saved = self.settings_store.get(guild_id)
                state.volume = saved["volume"]
                state.loop_mode = saved["loop_mode"]
            self.guilds[guild_id] = state
        return state

    async def settings_snapshot(self, guild_id: int) -> dict[str, object]:
        state = self.state_for(guild_id)
        return {"volume": state.volume, "loop_mode": state.loop_mode}

    async def apply_settings(self, guild_id: int, volume: int, loop_mode: str) -> dict[str, object]:
        if (
            type(volume) is not int
            or not 0 <= volume <= 200
            or not isinstance(loop_mode, str)
            or loop_mode not in {"off", "track", "queue"}
        ):
            raise MusicError("Invalid music settings.")
        state = self.state_for(guild_id)
        if self.settings_store is not None:
            try:
                saved = self.settings_store.set(guild_id, volume, loop_mode)
            except OSError:
                raise
        else:
            saved = {"volume": volume, "loop_mode": loop_mode}
        state.volume, state.loop_mode = volume, loop_mode
        voice_client = state.voice_client
        source = getattr(voice_client, "source", None) if voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = volume / 100
        return saved

    async def find_track(self, query: str, requester: str) -> Track:
        query = query.strip()
        if not query:
            raise MusicError("اكتب اسم أغنية أو رابط فيديو YouTube.")
        if len(query) > MAX_QUERY_LENGTH:
            raise MusicError(f"اجعل البحث أقصر من {MAX_QUERY_LENGTH} حرفًا.")

        if "://" in query:
            lookup = canonical_youtube_url(query)
        else:
            lookup = f"ytsearch1:{query}"

        try:
            return await asyncio.to_thread(_extract_track_sync, lookup, requester)
        except MusicError:
            raise
        except Exception as exc:
            logger.warning("YouTube search failed: %s", type(exc).__name__)
            raise MusicError(
                "تعذّر البحث في YouTube الآن؛ جرّب رابطًا أو عبارة بحث أخرى."
            ) from exc

    async def enqueue(
        self,
        guild: discord.Guild,
        voice_channel: discord.abc.Connectable,
        text_channel: discord.abc.Messageable,
        track: Track,
    ) -> int:
        state = self.state_for(guild.id)
        async with state.lock:
            if len(state.queue) >= MAX_QUEUE_SIZE:
                raise MusicError(f"قائمة الانتظار ممتلئة (الحد {MAX_QUEUE_SIZE}).")

            voice_client = state.voice_client or guild.voice_client
            if voice_client is not None and self._is_connected(voice_client):
                connected_channel = getattr(voice_client, "channel", None)
                if (
                    connected_channel is not None
                    and connected_channel.id != voice_channel.id
                ):
                    raise MusicError(
                        "البوت يشغّل الموسيقى في قناة صوتية أخرى حاليًا."
                    )
            else:
                member = getattr(guild, "me", None)
                if member is not None:
                    permissions = voice_channel.permissions_for(member)
                    if not permissions.connect or not permissions.speak:
                        raise MusicError(
                            "يحتاج البوت إلى صلاحيتَي Connect وSpeak في هذه القناة."
                        )
                try:
                    voice_client = await voice_channel.connect(
                        timeout=20, reconnect=True
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not connect to voice channel in guild %s: %s",
                        guild.id,
                        type(exc).__name__,
                    )
                    raise MusicError(
                        "تعذّر دخول القناة الصوتية؛ تحقق من صلاحيات البوت."
                    ) from exc

            state.voice_client = voice_client
            state.text_channel = text_channel
            channel_id = getattr(text_channel, "id", None)
            if state.panel_channel_id != channel_id:
                state.panel_message = None
                state.panel_channel_id = channel_id

            is_active = bool(
                state.current
                or state.starting
                or voice_client.is_playing()
                or voice_client.is_paused()
            )
            position = len(state.queue) + (2 if is_active else 1)
            state.queue.append(track)

        await self._start_next(guild.id)
        return position

    @staticmethod
    def _is_connected(voice_client: object) -> bool:
        checker = getattr(voice_client, "is_connected", None)
        return bool(checker()) if callable(checker) else True

    async def _start_next(self, guild_id: int) -> None:
        state = self.state_for(guild_id)
        while True:
            async with state.lock:
                voice_client = state.voice_client
                if (
                    voice_client is None
                    or not self._is_connected(voice_client)
                    or state.starting
                    or voice_client.is_playing()
                    or voice_client.is_paused()
                ):
                    return

                if state.loop_mode == "track" and state.current is not None:
                    track = state.current
                elif state.queue:
                    track = state.queue.popleft()
                    state.current = track
                else:
                    state.current = None
                    track = None

                if track is None:
                    state.starting = False
                    should_render_empty = True
                    token = state.generation
                else:
                    state.current = track
                    state.starting = True
                    token = state.generation
                    should_render_empty = False

            if should_render_empty:
                await self._render_panel(guild_id)
                return

            await self._render_panel(guild_id)
            try:
                audio_url = await asyncio.to_thread(
                    _resolve_audio_url_sync, track.url
                )
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        audio_url,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS,
                    ),
                    volume=state.volume / 100,
                )
            except Exception as exc:
                logger.warning(
                    "Could not prepare YouTube track %s: %s",
                    track.video_id,
                    type(exc).__name__,
                )
                async with state.lock:
                    if token != state.generation:
                        return
                    state.starting = False
                    state.current = None
                await self._notify(
                    state,
                    "تعذّر تشغيل هذه النتيجة؛ سأنتقل إلى الأغنية التالية.",
                )
                continue

            loop = asyncio.get_running_loop()

            def after(error: Exception | None) -> None:
                if error:
                    logger.warning(
                        "Audio playback failed in guild %s: %s",
                        guild_id,
                        type(error).__name__,
                    )

                def schedule_finish() -> None:
                    asyncio.create_task(
                        self._track_finished(guild_id, token, bool(error))
                    )

                try:
                    loop.call_soon_threadsafe(schedule_finish)
                except RuntimeError:
                    logger.info("Playback callback arrived after event-loop shutdown.")

            async with state.lock:
                if (
                    token != state.generation
                    or voice_client is not state.voice_client
                    or not self._is_connected(voice_client)
                ):
                    cleanup = getattr(source, "cleanup", None)
                    if callable(cleanup):
                        cleanup()
                    return
                state.starting = False
                try:
                    voice_client.play(source, after=after)
                except Exception as exc:
                    logger.warning(
                        "Could not start audio in guild %s: %s",
                        guild_id,
                        type(exc).__name__,
                    )
                    state.current = None
                    cleanup = getattr(source, "cleanup", None)
                    if callable(cleanup):
                        cleanup()
                    continue

            await self._render_panel(guild_id)
            return

    async def _track_finished(
        self, guild_id: int, token: int, failed: bool
    ) -> None:
        state = self.state_for(guild_id)
        async with state.lock:
            if token != state.generation:
                return

            current = state.current
            action = state.pending_action
            state.pending_action = None
            if action == "previous":
                state.current = None
            elif action == "skip":
                if current is not None:
                    state.history.append(current)
                    if state.loop_mode == "queue":
                        state.queue.append(current)
                state.current = None
            elif failed:
                if current is not None:
                    state.history.append(current)
                state.current = None
            elif state.loop_mode == "track" and current is not None:
                # _start_next will replay the current track.
                pass
            else:
                if current is not None:
                    state.history.append(current)
                    if state.loop_mode == "queue":
                        state.queue.append(current)
                state.current = None

        await self._render_panel(guild_id)
        await self._start_next(guild_id)

    async def authorize_member(
        self, guild_id: int, member: discord.Member
    ) -> GuildMusicState:
        state = self.state_for(guild_id)
        voice_client = state.voice_client
        if voice_client is None or not self._is_connected(voice_client):
            raise MusicError("لا توجد جلسة موسيقى في هذا السيرفر.")
        member_voice = getattr(member, "voice", None)
        member_channel = getattr(member_voice, "channel", None)
        bot_channel = getattr(voice_client, "channel", None)
        if member_channel is None or bot_channel is None or member_channel.id != bot_channel.id:
            raise MusicError("استخدم التحكم وأنت في قناة البوت الصوتية.")
        return state

    async def pause(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        voice_client = state.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            return "تم إيقاف الموسيقى مؤقتًا."
        raise MusicError("لا توجد أغنية تعمل لإيقافها مؤقتًا.")

    async def resume(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        voice_client = state.voice_client
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            return "استؤنف تشغيل الموسيقى."
        raise MusicError("لا توجد أغنية متوقفة مؤقتًا.")

    async def skip(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        async with state.lock:
            voice_client = state.voice_client
            if (
                voice_client is None
                or state.current is None
                or state.starting
                or not (
                    voice_client.is_playing() or voice_client.is_paused()
                )
            ):
                raise MusicError("لا توجد أغنية تعمل لتخطيها.")
            state.pending_action = "skip"
            voice_client.stop()
        return "تم تخطي الأغنية."

    async def previous(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        async with state.lock:
            voice_client = state.voice_client
            if (
                voice_client is None
                or state.current is None
                or state.starting
                or not (
                    voice_client.is_playing() or voice_client.is_paused()
                )
            ):
                raise MusicError("لا توجد أغنية تعمل للرجوع.")
            if not state.history:
                raise MusicError("لا توجد أغنية سابقة في السجل.")

            current = state.current
            previous_track = state.history.pop()
            state.queue.appendleft(current)
            state.queue.appendleft(previous_track)
            state.pending_action = "previous"
            voice_client.stop()
        return "تمت إعادة الأغنية السابقة."

    async def stop(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        async with state.lock:
            state.generation += 1
            state.starting = False
            state.pending_action = None
            state.queue.clear()
            state.current = None
            voice_client = state.voice_client
            state.voice_client = None
            if voice_client is not None:
                try:
                    if voice_client.is_playing() or voice_client.is_paused():
                        voice_client.stop()
                except Exception:
                    logger.debug("Voice client was already stopping.")

        if voice_client is not None:
            try:
                if self._is_connected(voice_client):
                    await voice_client.disconnect(force=True)
            except Exception as exc:
                logger.info(
                    "Could not disconnect voice client in guild %s: %s",
                    guild_id,
                    type(exc).__name__,
                )
        await self._render_panel(guild_id)
        return "تم إيقاف الموسيقى ومسح قائمة الانتظار."

    async def set_volume(self, guild_id: int, volume: int) -> int:
        if not 0 <= volume <= 200:
            raise MusicError("مستوى الصوت يجب أن يكون بين 0 و200.")
        state = self.state_for(guild_id)
        if self.settings_store is not None:
            self.settings_store.set(guild_id, volume, state.loop_mode)
        state.volume = volume
        voice_client = state.voice_client
        source = getattr(voice_client, "source", None) if voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = volume / 100
        return volume

    async def adjust_volume(self, guild_id: int, amount: int) -> int:
        state = self.state_for(guild_id)
        return await self.set_volume(
            guild_id, min(200, max(0, state.volume + amount))
        )

    async def cycle_loop(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        modes = ("off", "track", "queue")
        next_mode = modes[(modes.index(state.loop_mode) + 1) % len(modes)]
        if self.settings_store is not None:
            self.settings_store.set(guild_id, state.volume, next_mode)
        state.loop_mode = next_mode
        return {
            "off": "التكرار متوقف.",
            "track": "تكرار الأغنية الحالية.",
            "queue": "تكرار قائمة الانتظار.",
        }[state.loop_mode]

    async def shuffle(self, guild_id: int) -> str:
        state = self.state_for(guild_id)
        async with state.lock:
            if len(state.queue) < 2:
                raise MusicError("أضف أغنيتين على الأقل لخلط القائمة.")
            queued = list(state.queue)
            random.shuffle(queued)
            state.queue = deque(queued)
        return "تم خلط قائمة الانتظار."

    async def queue_snapshot(
        self, guild_id: int
    ) -> tuple[Track | None, list[Track], int, str]:
        state = self.state_for(guild_id)
        async with state.lock:
            return (
                state.current,
                list(state.queue),
                state.volume,
                state.loop_mode,
            )

    async def _render_panel(self, guild_id: int) -> None:
        state = self.state_for(guild_id)
        channel = state.text_channel
        if channel is None:
            return
        async with state.panel_lock:
            embed = self._panel_embed(state)
            view = MusicControls(self)
            message = state.panel_message
            if message is not None:
                try:
                    await message.edit(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                except discord.HTTPException:
                    state.panel_message = None
            try:
                state.panel_message = await channel.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                state.panel_channel_id = getattr(channel, "id", None)
            except (discord.Forbidden, discord.HTTPException):
                logger.info("Could not post music controls in guild %s.", guild_id)

    @staticmethod
    def _panel_embed(state: GuildMusicState) -> discord.Embed:
        current = state.current
        if current is None:
            embed = discord.Embed(
                title="مشغل الموسيقى",
                description="لا توجد أغنية تعمل حاليًا. استخدم `!play` لبدء التشغيل.",
                color=0x5865F2,
            )
        else:
            embed = discord.Embed(
                title="🎵 تعمل الآن",
                description=f"[{current.display_title}]({current.url})",
                color=0x5865F2,
            )
            embed.add_field(name="المدة", value=current.duration_text, inline=True)
            embed.add_field(name="طلبها", value=current.requester or "عضو", inline=True)
        loop_names = {"off": "متوقف", "track": "الأغنية", "queue": "القائمة"}
        embed.add_field(
            name="الصوت",
            value=f"{state.volume}% · التكرار: {loop_names[state.loop_mode]}",
            inline=False,
        )
        if state.queue:
            preview = "\n".join(
                f"{index}. {track.display_title}"
                for index, track in enumerate(list(state.queue)[:5], 1)
            )
            if len(state.queue) > 5:
                preview += f"\n… و{len(state.queue) - 5} أخرى"
            embed.add_field(name=f"التالي ({len(state.queue)})", value=preview, inline=False)
        embed.set_footer(text="تعمل قائمة الانتظار أثناء تشغيل البوت فقط.")
        return embed

    @staticmethod
    async def _notify(state: GuildMusicState, message: str) -> None:
        channel = state.text_channel
        if channel is None:
            return
        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.info("Could not send a music playback notification.")

    async def close(self) -> None:
        for guild_id in list(self.guilds):
            state = self.state_for(guild_id)
            voice_client = state.voice_client
            state.generation += 1
            state.current = None
            state.queue.clear()
            state.starting = False
            state.pending_action = None
            state.voice_client = None
            if voice_client is None:
                continue
            try:
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                if self._is_connected(voice_client):
                    await voice_client.disconnect(force=True)
            except Exception as exc:
                logger.info(
                    "Could not close voice client for guild %s: %s",
                    guild_id,
                    type(exc).__name__,
                )


class MusicControls(discord.ui.View):
    """Controls are guild-aware and only usable by members in the bot's voice channel."""

    def __init__(self, manager: MusicManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    async def _respond(
        self, interaction: discord.Interaction, operation
    ) -> None:
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.response.send_message(
                "أزرار الموسيقى تعمل داخل السيرفر فقط.", ephemeral=True
            )
            return
        try:
            await self.manager.authorize_member(
                interaction.guild_id, interaction.user
            )
            message = await operation(interaction.guild_id)
        except MusicError as exc:
            message = str(exc)
        await interaction.response.send_message(message, ephemeral=True)
        if interaction.guild_id is not None:
            await self.manager._render_panel(interaction.guild_id)

    @discord.ui.button(
        emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="music:previous", row=0
    )
    async def previous_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(interaction, self.manager.previous)

    @discord.ui.button(
        emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music:pause_resume", row=0
    )
    async def pause_resume_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild_id is None:
            await self._respond(interaction, self.manager.pause)
            return
        state = self.manager.state_for(interaction.guild_id)
        operation = self.manager.resume if (
            state.voice_client and state.voice_client.is_paused()
        ) else self.manager.pause
        await self._respond(interaction, operation)

    @discord.ui.button(
        emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip", row=0
    )
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(interaction, self.manager.skip)

    @discord.ui.button(
        emoji="📋", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=0
    )
    async def queue_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.response.send_message(
                "أزرار الموسيقى تعمل داخل السيرفر فقط.", ephemeral=True
            )
            return
        try:
            await self.manager.authorize_member(
                interaction.guild_id, interaction.user
            )
        except MusicError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        current, queued, volume, loop_mode = await self.manager.queue_snapshot(
            interaction.guild_id
        )
        lines = []
        if current:
            lines.append(f"▶️ الآن: {current.display_title} [{current.duration_text}]")
        lines.extend(
            f"{index}. {track.display_title} [{track.duration_text}]"
            for index, track in enumerate(queued[:10], 1)
        )
        if len(queued) > 10:
            lines.append(f"… و{len(queued) - 10} أغانٍ أخرى")
        if not lines:
            lines.append("قائمة الانتظار فارغة.")
        embed = discord.Embed(
            title="قائمة التشغيل",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
        )
        embed.set_footer(text=f"الصوت {volume}% · التكرار: {loop_mode}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0
    )
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(interaction, self.manager.stop)

    @discord.ui.button(
        emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="music:volume_down", row=1
    )
    async def volume_down_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(
            interaction, lambda guild_id: self.manager.adjust_volume(guild_id, -10)
        )

    @discord.ui.button(
        emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1
    )
    async def loop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(interaction, self.manager.cycle_loop)

    @discord.ui.button(
        emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=1
    )
    async def shuffle_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(interaction, self.manager.shuffle)

    @discord.ui.button(
        emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="music:volume_up", row=1
    )
    async def volume_up_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._respond(
            interaction, lambda guild_id: self.manager.adjust_volume(guild_id, 10)
        )


def setup_music_commands(
    bot: commands.Bot, manager: MusicManager
) -> commands.Bot:
    """Register guild-scoped music commands and persistent control handlers once."""
    if getattr(bot, "_music_commands_registered", False):
        return bot

    bot.add_view(MusicControls(manager))

    @bot.command(name="play", aliases=["شغل", "p"])
    @commands.guild_only()
    async def play(ctx: commands.Context, *, query: str | None = None) -> None:
        if not query:
            await ctx.reply(
                "اكتب اسم أغنية أو رابط فيديو YouTube. مثال: `!play nightcore`",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        member_voice = getattr(ctx.author, "voice", None)
        voice_channel = getattr(member_voice, "channel", None)
        if voice_channel is None:
            await ctx.reply(
                "ادخل قناة صوتية أولًا.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        progress = await ctx.reply(
            "🔎 أبحث في YouTube وأجهّز الأغنية…",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            track = await manager.find_track(query, ctx.author.display_name)
            position = await manager.enqueue(
                ctx.guild, voice_channel, ctx.channel, track
            )
        except MusicError as exc:
            await progress.edit(content=f"❌ {exc}", allowed_mentions=discord.AllowedMentions.none())
            return
        except Exception:
            logger.exception("Unexpected error while handling a music request.")
            await progress.edit(
                content="حدث خطأ أثناء تجهيز الموسيقى. حاول مجددًا لاحقًا.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await progress.edit(
            content=(
                f"🎶 أُضيفت **{track.display_title}** إلى قائمة الانتظار "
                f"(المركز {position})."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="queue", aliases=["قائمة", "q"])
    @commands.guild_only()
    async def show_queue(ctx: commands.Context) -> None:
        current, queued, volume, loop_mode = await manager.queue_snapshot(ctx.guild.id)
        lines = []
        if current:
            lines.append(
                f"▶️ الآن: **{current.display_title}** [{current.duration_text}]"
            )
        lines.extend(
            f"{index}. **{track.display_title}** [{track.duration_text}]"
            for index, track in enumerate(queued[:10], 1)
        )
        if len(queued) > 10:
            lines.append(f"… و{len(queued) - 10} أغانٍ أخرى")
        if not lines:
            lines.append("قائمة الانتظار فارغة.")
        embed = discord.Embed(
            title="قائمة التشغيل",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
        )
        embed.set_footer(text=f"الصوت {volume}% · التكرار: {loop_mode}")
        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="nowplaying", aliases=["الان", "np"])
    @commands.guild_only()
    async def now_playing(ctx: commands.Context) -> None:
        current, _, _, _ = await manager.queue_snapshot(ctx.guild.id)
        if current is None:
            await ctx.reply(
                "لا توجد أغنية تعمل حاليًا.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.reply(
            f"🎵 تعمل الآن: [{current.display_title}]({current.url}) "
            f"[{current.duration_text}] — طلبها {current.requester}.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="pause", aliases=["ايقاف_مؤقت"])
    @commands.guild_only()
    async def pause(ctx: commands.Context) -> None:
        await _run_text_control(ctx, manager, manager.pause)

    @bot.command(name="resume", aliases=["استئناف"])
    @commands.guild_only()
    async def resume(ctx: commands.Context) -> None:
        await _run_text_control(ctx, manager, manager.resume)

    @bot.command(name="skip", aliases=["تخطي", "s"])
    @commands.guild_only()
    async def skip(ctx: commands.Context) -> None:
        await _run_text_control(ctx, manager, manager.skip)

    @bot.command(name="previous", aliases=["السابق", "back"])
    @commands.guild_only()
    async def previous(ctx: commands.Context) -> None:
        await _run_text_control(ctx, manager, manager.previous)

    @bot.command(name="stop", aliases=["ايقاف", "اطلع", "leave"])
    @commands.guild_only()
    async def stop(ctx: commands.Context) -> None:
        await _run_text_control(ctx, manager, manager.stop)

    @bot.command(name="volume", aliases=["صوت", "vol"])
    @commands.guild_only()
    async def volume(ctx: commands.Context, amount: int | None = None) -> None:
        try:
            await manager.authorize_member(ctx.guild.id, ctx.author)
            state = manager.state_for(ctx.guild.id)
            if amount is None:
                message = f"مستوى الصوت الحالي: {state.volume}%."
            else:
                value = await manager.set_volume(ctx.guild.id, amount)
                message = f"تم ضبط مستوى الصوت إلى {value}%."
        except MusicError as exc:
            message = str(exc)
        await ctx.reply(
            message,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="music_help", aliases=["موسيقى_help", "music"])
    async def music_help(ctx: commands.Context) -> None:
        await ctx.reply(
            "**أوامر الموسيقى**\n"
            "`!play <اسم أو رابط YouTube>` — تشغيل أو إضافة أغنية.\n"
            "`!queue` — عرض قائمة الانتظار.\n"
            "`!nowplaying` — عرض الأغنية الحالية.\n"
            "`!pause` / `!resume` — إيقاف مؤقت أو استئناف.\n"
            "`!skip` / `!previous` — تخطٍ أو رجوع.\n"
            "`!volume [0-200]` — عرض أو ضبط الصوت.\n"
            "`!stop` — مسح القائمة والخروج من القناة.\n"
            "تظهر أزرار التحكم في لوحة التشغيل. الأزرار مخصصة لأعضاء قناة البوت الصوتية.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot._music_commands_registered = True
    return bot


async def _run_text_control(
    ctx: commands.Context,
    manager: MusicManager,
    operation,
) -> None:
    try:
        await manager.authorize_member(ctx.guild.id, ctx.author)
        message = await operation(ctx.guild.id)
    except MusicError as exc:
        message = str(exc)
    await ctx.reply(
        message,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )