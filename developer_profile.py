"""Public developer profile replies and the !dev command."""

from __future__ import annotations

import logging
import random
import re
import unicodedata
from typing import Literal

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)

DeveloperLanguage = Literal["ar", "en", "mixed"]

_ARABIC_DEV_QUESTION_PHRASES = (
    "من مطورك",
    "من هو مطورك",
    "من مطور البوت",
    "من هو مطور البوت",
    "من المطور",
    "من هو المطور",
    "من صنعك",
    "من صانعك",
    "من هو صانعك",
    "من مبرمجك",
    "من مبرمج البوت",
    "منو مطورك",
    "منو المطور",
    "منو اللي سوالك",
    "منو اللي سواك",
    "من اللي سوالك",
    "من اللي سواك",
    "من الذي صنعك",
    "مين مطورك",
    "مين هو مطورك",
    "مين المطور",
    "مين صنعك",
    "مين صانعك",
    "مين مبرمجك",
    "مين عملك",
    "من صممك",
    "من عمل البوت",
)
_ENGLISH_DEV_QUESTION_PHRASES = (
    "who made you",
    "who made this bot",
    "who made the bot",
    "who is your developer",
    "who s your developer",
    "who is the developer",
    "who is the bot developer",
    "who developed you",
    "who created you",
    "who created this bot",
    "who is your creator",
    "who built you",
    "who programmed you",
    "who wrote your code",
    "who is your owner",
    "who owns you",
    "who is your master",
)


def _normalize_question(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ٱ": "ا",
                "ى": "ي",
                "ـ": "",
            }
        )
    )
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalized)).strip()


def developer_question_language(question: str) -> DeveloperLanguage | None:
    """Return the reply language only for a clear question about the developer."""
    normalized = _normalize_question(question)
    if not any(
        phrase in normalized
        for phrase in (
            *_ARABIC_DEV_QUESTION_PHRASES,
            *_ENGLISH_DEV_QUESTION_PHRASES,
        )
    ):
        return None

    has_arabic = any("\u0600" <= character <= "\u06ff" for character in question)
    has_english = any(character.isascii() and character.isalpha() for character in question)
    if has_arabic and has_english:
        return "mixed"
    return "ar" if has_arabic else "en"


def developer_question_reply(
    question: str,
    developer_id: int,
    developer_name: str,
) -> str | None:
    """Create a short, factual answer without making an AI provider request."""
    language = developer_question_language(question)
    if language is None:
        return None

    mention = f"<@{developer_id}>"
    if language == "ar":
        responses = (
            f"مطوري هو {mention}، واسمه على Discord {developer_name}.",
            f"الذي طوّرني هو {mention} ({developer_name}).",
            f"مطور بوت DeepSeek هو {mention}، حسابه {developer_name}.",
        )
    elif language == "en":
        responses = (
            f"My developer is {mention} (@{developer_name}).",
            f"This bot was developed by {mention} (@{developer_name}).",
            f"The developer of this bot is {mention} (@{developer_name}).",
        )
    else:
        responses = (
            f"مطوري / My developer is {mention} (@{developer_name}).",
            f"مطور البوت / Bot developer: {mention} (@{developer_name}).",
        )
    return random.choice(responses)


def setup_developer_profile_command(
    bot,
    developer_id: int,
    developer_name: str,
):
    """Register a public !dev profile command once."""
    existing = getattr(bot, "_developer_profile_command", None)
    if existing is not None:
        return existing

    @bot.command(
        name="dev",
        aliases=["مطور", "developer", "مطوري", "creator"],
    )
    async def developer_info(ctx):
        language: DeveloperLanguage = (
            "ar"
            if any("\u0600" <= character <= "\u06ff" for character in ctx.invoked_with)
            else "en"
        )
        try:
            developer = await bot.fetch_user(developer_id)
        except Exception:
            logger.warning(
                "Could not fetch the developer's Discord profile for !dev; "
                "using the configured public name."
            )
            developer = bot.get_user(developer_id)

        account_name = getattr(developer, "name", None) or developer_name
        display_name = (
            getattr(developer, "global_name", None)
            or getattr(developer, "display_name", None)
            or account_name
        )
        display_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(str(display_name))
        )
        account_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(str(account_name))
        )
        mention = f"<@{developer_id}>"

        if language == "ar":
            embed = discord.Embed(
                title="👑 مطور البوت",
                description="الملف العام لمطور بوت DeepSeek.",
                color=0xFFD700,
            )
            fields = (
                ("👤 الاسم", f"**{display_name}**\n`@{account_name}`"),
                ("🔗 المنشن", mention),
                ("🆔 المعرّف", f"`{developer_id}`"),
                ("🎭 الدور", "مطور البوت"),
            )
            created_label = "📅 تاريخ إنشاء الحساب"
            footer = f"{developer_name} | مطور البوت"
        else:
            embed = discord.Embed(
                title="👑 Bot Developer",
                description="Public Discord profile for the DeepSeek bot developer.",
                color=0xFFD700,
            )
            fields = (
                ("👤 Name", f"**{display_name}**\n`@{account_name}`"),
                ("🔗 Mention", mention),
                ("🆔 ID", f"`{developer_id}`"),
                ("🎭 Role", "Bot developer"),
            )
            created_label = "📅 Account created"
            footer = f"{developer_name} | Bot Developer"

        for name, value in fields:
            embed.add_field(name=name, value=value, inline=True)

        created_at = getattr(developer, "created_at", None)
        if created_at is not None:
            timestamp = int(created_at.timestamp())
            embed.add_field(
                name=created_label,
                value=f"<t:{timestamp}:D> · <t:{timestamp}:R>",
                inline=False,
            )

        avatar = getattr(developer, "display_avatar", None)
        avatar_url = getattr(avatar, "url", None)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        banner = getattr(developer, "banner", None)
        banner_url = getattr(banner, "url", None)
        if banner_url:
            embed.set_image(url=banner_url)
        embed.set_footer(text=footer)

        await ctx.reply(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot._developer_profile_command = developer_info
    return developer_info
