"""Per-server reply style settings, separate from member emotion profiles."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


STYLE_LEVELS = {
    "low": ("خفيف", "لطف هادئ مع استخدام قليل للإيموجي."),
    "medium": ("متوسط", "دفء وود واضحان مع إيموجي معتدلة."),
    "high": ("عالٍ", "دفء وحماس أكثر، مع بقاء الرد طبيعيًا وغير مبالغ."),
}
STYLE_MODES = {
    "love": (
        "حنون",
        "استخدم تعبيرًا دافئًا ومقدرًا، من دون افتراض علاقة عاطفية أو استخدام "
        "ألقاب حميمة إلا إذا طلبها المستخدم.",
    ),
    "friendly": (
        "ودود",
        "كن ودودًا وخفيف الظل، واستخدم مزاحًا لطيفًا عندما يناسب السياق.",
    ),
    "caring": (
        "مهتم",
        "كن متفهمًا ومساندًا، من دون التطفل أو الضغط على المستخدم.",
    ),
}
_DEFAULT_SETTINGS = {"enabled": False, "level": "medium", "mode": "love"}
_SETTING_KEYS = frozenset(_DEFAULT_SETTINGS)


class ServerEmotionStyleStore:
    """Persist developer-selected reply tone per guild; never stores member data."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict) or not isinstance(data.get("guilds"), dict):
            raise ValueError(
                f"Invalid server emotion style at {self.path}: expected a guilds object"
            )

        for guild_id, saved_settings in data["guilds"].items():
            if not isinstance(guild_id, str) or not guild_id.isdecimal():
                raise ValueError(f"Invalid guild ID in server emotion style: {guild_id}")
            self._guilds[guild_id] = self._validated_settings(
                saved_settings, guild_id
            )

    @staticmethod
    def _validated_settings(settings: dict, guild_id: str) -> dict:
        if not isinstance(settings, dict) or set(settings) - _SETTING_KEYS:
            raise ValueError(f"Invalid server emotion style settings for guild {guild_id}")
        result = dict(_DEFAULT_SETTINGS)
        result.update(settings)
        if (
            type(result["enabled"]) is not bool
            or not isinstance(result["level"], str)
            or result["level"] not in STYLE_LEVELS
            or not isinstance(result["mode"], str)
            or result["mode"] not in STYLE_MODES
        ):
            raise ValueError(f"Invalid server emotion style settings for guild {guild_id}")
        return result

    @staticmethod
    def _guild_key(guild_id: int) -> str:
        if type(guild_id) is not int or guild_id <= 0:
            raise ValueError("guild_id must be a positive integer")
        return str(guild_id)

    def get_settings(self, guild_id: int) -> dict:
        key = self._guild_key(guild_id)
        with self._lock:
            return dict(self._guilds.get(key, _DEFAULT_SETTINGS))

    def _update(self, guild_id: int, **changes) -> dict:
        key = self._guild_key(guild_id)
        with self._lock:
            settings = dict(self._guilds.get(key, _DEFAULT_SETTINGS))
            settings.update(changes)
            settings = self._validated_settings(settings, key)
            updated_guilds = dict(self._guilds)
            updated_guilds[key] = settings
            self._save(updated_guilds)
            self._guilds = updated_guilds
            return dict(settings)

    def _save(self, guilds: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = file.name
                json.dump({"guilds": guilds}, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
        finally:
            if temp_path is not None:
                os.unlink(temp_path)

    def set_enabled(self, guild_id: int, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return self._update(guild_id, enabled=enabled)

    def set_level(self, guild_id: int, level: str) -> dict:
        if level not in STYLE_LEVELS:
            raise ValueError(f"Unknown emotion style level: {level}")
        return self._update(guild_id, level=level)

    def set_mode(self, guild_id: int, mode: str) -> dict:
        if mode not in STYLE_MODES:
            raise ValueError(f"Unknown emotion style mode: {mode}")
        return self._update(guild_id, mode=mode)

    def get_prompt_context(self, guild_id: int | None) -> str:
        if guild_id is None:
            return ""
        settings = self.get_settings(guild_id)
        if not settings["enabled"]:
            return ""

        level_name, level_description = STYLE_LEVELS[settings["level"]]
        mode_name, mode_description = STYLE_MODES[settings["mode"]]
        return (
            "\n\nنبرة الرد العامة لهذا السيرفر، ضبطها مطور البوت:\n"
            f"- مستوى الدفء: {level_name} — {level_description}\n"
            f"- النمط: {mode_name} — {mode_description}\n"
            "- طبّق هذه الإعدادات على صياغة الرد فقط. لا تستنتج مشاعر المستخدم، "
            "ولا تحفظها أو تغيّر موافقته الشخصية على ملف المشاعر."
        )


def setup_server_emotion_style_commands(
    bot: commands.Bot,
    store: ServerEmotionStyleStore,
    developer_id: int,
) -> None:
    """Register the developer-only /emotions command."""

    @bot.tree.command(
        name="emotions",
        description="التحكم بأسلوب الرد العام في هذا السيرفر (للمطور فقط)",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        action="اختر ما تريد تعديله",
        level="مستوى الدفء",
        mode="أسلوب الرد",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="تفعيل الأسلوب العام", value="enable"),
            app_commands.Choice(name="إيقاف الأسلوب العام", value="disable"),
            app_commands.Choice(name="تغيير المستوى", value="set_level"),
            app_commands.Choice(name="تغيير النمط", value="set_mode"),
            app_commands.Choice(name="عرض الإعدادات", value="show"),
        ],
        level=[
            app_commands.Choice(name="خفيف", value="low"),
            app_commands.Choice(name="متوسط", value="medium"),
            app_commands.Choice(name="عالٍ", value="high"),
        ],
        mode=[
            app_commands.Choice(name="حنون", value="love"),
            app_commands.Choice(name="ودود", value="friendly"),
            app_commands.Choice(name="مهتم", value="caring"),
        ],
    )
    async def emotions(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        level: app_commands.Choice[str] | None = None,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.user.id != developer_id:
            await interaction.response.send_message(
                "هذا الأمر خاص بمطور البوت.",
                ephemeral=True,
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "استخدم هذا الأمر داخل السيرفر المطلوب.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        if action.value == "enable":
            settings = store.set_enabled(guild_id, True)
            embed = discord.Embed(
                title="تم تفعيل أسلوب الرد العام",
                description=(
                    "سيؤثر هذا على نبرة ردود الذكاء الاصطناعي في هذا السيرفر فقط. "
                    "ملفات المشاعر الشخصية تبقى اختيارية لكل عضو."
                ),
                color=0xFF69B4,
            )
            embed.add_field(
                name="المستوى",
                value=STYLE_LEVELS[settings["level"]][0],
                inline=True,
            )
            embed.add_field(
                name="النمط",
                value=STYLE_MODES[settings["mode"]][0],
                inline=True,
            )
        elif action.value == "disable":
            store.set_enabled(guild_id, False)
            embed = discord.Embed(
                title="تم إيقاف أسلوب الرد العام",
                description=(
                    "عادت ردود الذكاء الاصطناعي إلى النبرة المعتادة. "
                    "لم تتغير ملفات المشاعر الشخصية للأعضاء."
                ),
                color=0xFFA500,
            )
        elif action.value == "set_level":
            if level is None:
                await interaction.followup.send(
                    "اختر مستوى الدفء أولًا.",
                    ephemeral=True,
                )
                return
            settings = store.set_level(guild_id, level.value)
            embed = discord.Embed(
                title="تم تحديث مستوى أسلوب الرد",
                description=f"المستوى الجديد: **{STYLE_LEVELS[settings['level']][0]}**",
                color=0xFF69B4,
            )
        elif action.value == "set_mode":
            if mode is None:
                await interaction.followup.send(
                    "اختر نمط الرد أولًا.",
                    ephemeral=True,
                )
                return
            settings = store.set_mode(guild_id, mode.value)
            embed = discord.Embed(
                title="تم تحديث نمط أسلوب الرد",
                description=f"النمط الجديد: **{STYLE_MODES[settings['mode']][0]}**",
                color=0xFF69B4,
            )
        else:
            settings = store.get_settings(guild_id)
            embed = discord.Embed(
                title="إعدادات أسلوب الرد العام",
                description=(
                    f"في سيرفر **{discord.utils.escape_markdown(interaction.guild.name)}**"
                    if interaction.guild is not None
                    else "إعدادات السيرفر الحالي"
                ),
                color=0xFF69B4,
            )
            embed.add_field(
                name="الحالة",
                value="مفعّل" if settings["enabled"] else "متوقف",
                inline=True,
            )
            embed.add_field(
                name="المستوى",
                value=STYLE_LEVELS[settings["level"]][0],
                inline=True,
            )
            embed.add_field(
                name="النمط",
                value=STYLE_MODES[settings["mode"]][0],
                inline=True,
            )
            embed.set_footer(
                text="هذا الإعداد يغيّر النبرة فقط ولا يفعّل ملفات الأعضاء الشخصية."
            )

        await interaction.followup.send(embed=embed, ephemeral=True)