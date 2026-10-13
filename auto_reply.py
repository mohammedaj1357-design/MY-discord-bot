"""Per-guild automatic replies with Minecraft server connection details."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
import discord
from discord import app_commands

from collect_notifications import normalize_invite_target


logger = logging.getLogger(__name__)

DEFAULT_AUTOREPLY_SETTINGS = {
    "enabled": False,
    "keywords": ["ip", "اي بي", "ايبي", "server ip", "عنوان السيرفر"],
    "java_ip": "valkrossmc.online",
    "java_port": 25565,
    "bedrock_ip": "valkrossmc.online",
    "bedrock_port": 27108,
    "server_name": "Valkross",
    "discord_link": "",
    "cooldown_seconds": 30,
    "custom_message": None,
    "use_custom_message": False,
    "show_status": True,
    "show_players": True,
    "show_buttons": True,
    "server_icon": None,
    "footer_text": "❤️ حياك تنورنا",
}
_SETTING_KEYS = set(DEFAULT_AUTOREPLY_SETTINGS)
_PORT_KEYS = {"java_port", "bedrock_port"}
_ADDRESS_KEYS = {"java_ip", "bedrock_ip"}
_MAX_COOLDOWN_SECONDS = 86_400
_MIN_COOLDOWN_SECONDS = 5
_MAX_CUSTOM_MESSAGE_LENGTH = 1_800
_MAX_AUTO_REPLY_KEYWORDS = 20
_MAX_KEYWORD_PREVIEW_LENGTH = 1_000
_KEYWORD_HISTORY_PAGE_SIZE = 10
_AUTO_REPLY_CONFIRMATION_TIMEOUT_SECONDS = 120
_KEYWORD_HISTORY_ACTIONS = {"added", "removed", "restored_defaults"}
_VALID_HOST_LABEL = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]+)\}")
_ALLOWED_TEMPLATE_KEYS = {
    "java_ip",
    "java_port",
    "bedrock_ip",
    "bedrock_port",
    "server_name",
    "user",
}
_SERVER_STATUS_URL = "https://api.mcsrvstat.us/3/"
_SERVER_STATUS_TIMEOUT_SECONDS = 5
_SERVER_STATUS_CACHE_SECONDS = 20


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )


def _copy_settings(settings: dict) -> dict:
    return {
        **settings,
        "keywords": list(settings["keywords"]),
    }


def _normalize_image_url(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value.strip()) > 2_048:
        raise ValueError("أدخل رابط صورة HTTPS صالحًا.")
    target = value.strip()
    try:
        parsed = urlsplit(target)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("أدخل رابط صورة HTTPS صالحًا.") from error
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path
        or any(character.isspace() for character in target)
    ):
        raise ValueError("أدخل رابط صورة HTTPS صالحًا.")
    return target


def _guild_key(guild_id: int) -> str:
    if type(guild_id) is not int or guild_id <= 0:
        raise ValueError("guild_id must be a positive Discord ID")
    return str(guild_id)


def _validated_keyword_history_entry(entry: dict, guild_id: str) -> dict:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"actor_id", "action", "phrase", "timestamp"}
    ):
        raise ValueError(f"Invalid auto-reply keyword history for guild {guild_id}")

    actor_id = entry["actor_id"]
    action = entry["action"]
    phrase = entry["phrase"]
    timestamp = entry["timestamp"]
    if (
        not isinstance(actor_id, str)
        or not actor_id.isdecimal()
        or int(actor_id) <= 0
        or not isinstance(action, str)
        or action not in _KEYWORD_HISTORY_ACTIONS
        or type(timestamp) is not int
        or timestamp < 0
    ):
        raise ValueError(f"Invalid auto-reply keyword history for guild {guild_id}")

    if action in {"added", "removed"}:
        if (
            not isinstance(phrase, str)
            or not phrase.strip()
            or len(phrase.strip()) > 80
        ):
            raise ValueError(
                f"Invalid auto-reply keyword history phrase for guild {guild_id}"
            )
        phrase = phrase.strip()
    elif phrase is not None:
        raise ValueError(
            f"Invalid auto-reply keyword history phrase for guild {guild_id}"
        )

    return {
        "actor_id": actor_id,
        "action": action,
        "phrase": phrase,
        "timestamp": timestamp,
    }


def _normalize_server_address(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("أدخل عنوان IP أو اسم نطاق صالحًا.")
    address = value.strip()
    if (
        not address
        or len(address) > 253
        or any(character.isspace() for character in address)
        or any(character in address for character in ("/", "\\", "@", "?", "#"))
    ):
        raise ValueError("أدخل عنوان IP أو اسم نطاق صالحًا دون رابط أو منفذ.")

    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        pass

    hostname = address.rstrip(".")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("اسم النطاق غير صالح.") from error
    labels = ascii_hostname.split(".")
    if (
        not ascii_hostname
        or len(ascii_hostname) > 253
        or any(
            len(label) > 63 or not _VALID_HOST_LABEL.fullmatch(label)
            for label in labels
        )
    ):
        raise ValueError("أدخل عنوان IP أو اسم نطاق صالحًا دون رابط أو منفذ.")
    return hostname


def _validated_settings(settings: dict, guild_id: str) -> dict:
    if not isinstance(settings, dict) or set(settings) != _SETTING_KEYS:
        raise ValueError(f"Invalid auto-reply settings for guild {guild_id}")

    result = dict(settings)
    if type(result["enabled"]) is not bool:
        raise ValueError(f"Invalid auto-reply enabled flag for guild {guild_id}")

    keywords = result["keywords"]
    if (
        not isinstance(keywords, list)
        or len(keywords) > _MAX_AUTO_REPLY_KEYWORDS
        or any(
            not isinstance(keyword, str)
            or not keyword.strip()
            or len(keyword.strip()) > 80
            for keyword in keywords
        )
    ):
        raise ValueError(f"Invalid auto-reply keywords for guild {guild_id}")
    normalized_keywords = [keyword.strip() for keyword in keywords]
    if len({_normalized_text(keyword) for keyword in normalized_keywords}) != len(
        normalized_keywords
    ):
        raise ValueError(f"Duplicate auto-reply keywords for guild {guild_id}")
    result["keywords"] = normalized_keywords

    for key in _ADDRESS_KEYS:
        result[key] = _normalize_server_address(result[key])
    for key in _PORT_KEYS:
        port = result[key]
        if type(port) is not int or not 1 <= port <= 65_535:
            raise ValueError(f"Invalid {key} for guild {guild_id}")

    server_name = result["server_name"]
    if (
        not isinstance(server_name, str)
        or not server_name.strip()
        or len(server_name.strip()) > 80
        or "\n" in server_name
        or "\r" in server_name
    ):
        raise ValueError(f"Invalid auto-reply server name for guild {guild_id}")
    result["server_name"] = server_name.strip()

    discord_link = result["discord_link"]
    if not isinstance(discord_link, str):
        raise ValueError(f"Invalid auto-reply invite link for guild {guild_id}")
    if discord_link.strip():
        result["discord_link"] = normalize_invite_target(discord_link)
    else:
        result["discord_link"] = ""

    cooldown = result["cooldown_seconds"]
    if (
        type(cooldown) is not int
        or not _MIN_COOLDOWN_SECONDS <= cooldown <= _MAX_COOLDOWN_SECONDS
    ):
        raise ValueError(f"Invalid auto-reply cooldown for guild {guild_id}")

    for key in ("use_custom_message", "show_status", "show_players", "show_buttons"):
        if type(result[key]) is not bool:
            raise ValueError(f"Invalid {key} for guild {guild_id}")

    custom_message = result["custom_message"]
    if custom_message is not None:
        if (
            not isinstance(custom_message, str)
            or not custom_message.strip()
            or len(custom_message) > _MAX_CUSTOM_MESSAGE_LENGTH
        ):
            raise ValueError(f"Invalid custom message for guild {guild_id}")
        unknown_tokens = {
            token
            for token in _TEMPLATE_TOKEN.findall(custom_message)
            if token not in _ALLOWED_TEMPLATE_KEYS
        }
        if unknown_tokens:
            raise ValueError(f"Invalid custom message placeholders for guild {guild_id}")
        result["custom_message"] = custom_message.strip()
    if result["use_custom_message"] and not result["custom_message"]:
        raise ValueError(f"Missing custom message for guild {guild_id}")

    result["server_icon"] = _normalize_image_url(result["server_icon"])
    footer_text = result["footer_text"]
    if (
        not isinstance(footer_text, str)
        or not footer_text.strip()
        or len(footer_text.strip()) > 100
        or "\n" in footer_text
        or "\r" in footer_text
    ):
        raise ValueError(f"Invalid auto-reply footer for guild {guild_id}")
    result["footer_text"] = footer_text.strip()
    return result


class AutoReplyStore:
    """Atomically persist auto-reply settings separately for each guild."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        self._keyword_history: dict[str, list[dict]] = {}
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        schema_version = (
            payload.get("schema_version") if isinstance(payload, dict) else None
        )
        expected_keys = (
            {"schema_version", "guilds", "keyword_history"}
            if schema_version == 3
            else {"schema_version", "guilds"}
        )
        if (
            not isinstance(payload, dict)
            or type(schema_version) is not int
            or schema_version not in {1, 2, 3}
            or set(payload) != expected_keys
            or not isinstance(payload["guilds"], dict)
        ):
            raise ValueError(f"Invalid auto-reply settings at {self.path}")

        for raw_guild_id, settings in payload["guilds"].items():
            if (
                not isinstance(raw_guild_id, str)
                or not raw_guild_id.isdecimal()
                or int(raw_guild_id) <= 0
            ):
                raise ValueError("Invalid guild ID in auto-reply settings")
            if schema_version == 1 and isinstance(settings, dict):
                settings = {
                    **DEFAULT_AUTOREPLY_SETTINGS,
                    **settings,
                }
            self._guilds[raw_guild_id] = _validated_settings(
                settings,
                raw_guild_id,
            )

        if schema_version == 3:
            keyword_history = payload["keyword_history"]
            if not isinstance(keyword_history, dict):
                raise ValueError(f"Invalid auto-reply keyword history at {self.path}")
            for raw_guild_id, events in keyword_history.items():
                if (
                    not isinstance(raw_guild_id, str)
                    or not raw_guild_id.isdecimal()
                    or int(raw_guild_id) <= 0
                    or not isinstance(events, list)
                ):
                    raise ValueError(
                        f"Invalid auto-reply keyword history at {self.path}"
                    )
                self._keyword_history[raw_guild_id] = [
                    _validated_keyword_history_entry(event, raw_guild_id)
                    for event in events
                ]

    def get_settings(self, guild_id: int) -> dict:
        key = _guild_key(guild_id)
        with self._lock:
            settings = self._guilds.get(key, DEFAULT_AUTOREPLY_SETTINGS)
            return _copy_settings(settings)

    def _save(
        self,
        guilds: dict[str, dict],
        keyword_history: dict[str, list[dict]] | None = None,
    ) -> None:
        history = (
            self._keyword_history
            if keyword_history is None
            else keyword_history
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = file.name
                json.dump(
                    {
                        "schema_version": 3,
                        "guilds": {
                            guild_id: guilds[guild_id]
                            for guild_id in sorted(guilds, key=int)
                        },
                        "keyword_history": {
                            guild_id: history[guild_id]
                            for guild_id in sorted(history, key=int)
                        },
                    },
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                os.unlink(temporary_path)

    def update(self, guild_id: int, **changes) -> dict:
        key = _guild_key(guild_id)
        if not changes or set(changes) - _SETTING_KEYS:
            raise ValueError("Unknown or empty auto-reply settings update")
        with self._lock:
            current = self._guilds.get(key, DEFAULT_AUTOREPLY_SETTINGS)
            updated_settings = _copy_settings(current)
            updated_settings.update(changes)
            updated_settings = _validated_settings(updated_settings, key)
            updated = dict(self._guilds)
            updated[key] = updated_settings
            self._save(updated)
            self._guilds = updated
            return _copy_settings(updated_settings)

    def set_enabled(self, guild_id: int, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return self.update(guild_id, enabled=enabled)

    def get_keyword_history(self, guild_id: int) -> list[dict]:
        key = _guild_key(guild_id)
        with self._lock:
            return [
                dict(event)
                for event in self._keyword_history.get(key, [])
            ]

    def update_keywords(
        self,
        guild_id: int,
        keywords: list[str],
        *,
        actor_id: int,
        action: str,
        phrase: str | None = None,
    ) -> dict:
        key = _guild_key(guild_id)
        actor_key = _guild_key(actor_id)
        if not isinstance(action, str) or action not in _KEYWORD_HISTORY_ACTIONS:
            raise ValueError("Unknown auto-reply keyword history action")

        if action in {"added", "removed"}:
            if (
                not isinstance(phrase, str)
                or not phrase.strip()
                or len(phrase.strip()) > 80
            ):
                raise ValueError("Invalid auto-reply keyword history phrase")
            phrase = phrase.strip()
        elif phrase is not None:
            raise ValueError("Default restore history cannot include a phrase")

        with self._lock:
            current = self._guilds.get(key, DEFAULT_AUTOREPLY_SETTINGS)
            updated_settings = _copy_settings(current)
            updated_settings["keywords"] = keywords
            updated_settings = _validated_settings(updated_settings, key)
            old_keywords = current["keywords"]
            new_keywords = updated_settings["keywords"]

            if action == "added":
                normalized_phrase = _normalized_text(phrase)
                if (
                    any(
                        _normalized_text(keyword) == normalized_phrase
                        for keyword in old_keywords
                    )
                    or not any(
                        _normalized_text(keyword) == normalized_phrase
                        for keyword in new_keywords
                    )
                ):
                    raise ValueError("Keyword history does not match the update")
            elif action == "removed":
                normalized_phrase = _normalized_text(phrase)
                if (
                    not any(
                        _normalized_text(keyword) == normalized_phrase
                        for keyword in old_keywords
                    )
                    or any(
                        _normalized_text(keyword) == normalized_phrase
                        for keyword in new_keywords
                    )
                ):
                    raise ValueError("Keyword history does not match the update")
            elif new_keywords != DEFAULT_AUTOREPLY_SETTINGS["keywords"]:
                raise ValueError("Default restore must use the built-in keywords")

            if new_keywords == old_keywords:
                return _copy_settings(current)

            event = {
                "actor_id": actor_key,
                "action": action,
                "phrase": phrase,
                "timestamp": int(time.time()),
            }
            updated_history = dict(self._keyword_history)
            updated_history[key] = [
                *self._keyword_history.get(key, []),
                event,
            ]
            updated = dict(self._guilds)
            updated[key] = updated_settings
            self._save(updated, updated_history)
            self._guilds = updated
            self._keyword_history = updated_history
            return _copy_settings(updated_settings)

    def restore_default_keywords(
        self,
        guild_id: int,
        *,
        actor_id: int | None = None,
    ) -> dict:
        default_keywords = list(DEFAULT_AUTOREPLY_SETTINGS["keywords"])
        if actor_id is None:
            return self.update(guild_id, keywords=default_keywords)
        return self.update_keywords(
            guild_id,
            default_keywords,
            actor_id=actor_id,
            action="restored_defaults",
        )

    def reset(self, guild_id: int, *, actor_id: int | None = None) -> dict:
        key = _guild_key(guild_id)
        actor_key = _guild_key(actor_id) if actor_id is not None else None
        with self._lock:
            current = self._guilds.get(key, DEFAULT_AUTOREPLY_SETTINGS)
            updated_history = self._keyword_history
            if (
                actor_key is not None
                and current["keywords"] != DEFAULT_AUTOREPLY_SETTINGS["keywords"]
            ):
                updated_history = dict(self._keyword_history)
                updated_history[key] = [
                    *self._keyword_history.get(key, []),
                    {
                        "actor_id": actor_key,
                        "action": "restored_defaults",
                        "phrase": None,
                        "timestamp": int(time.time()),
                    },
                ]
            if key in self._guilds:
                updated = dict(self._guilds)
                updated.pop(key)
                self._save(updated, updated_history)
                self._guilds = updated
                self._keyword_history = updated_history
            elif updated_history is not self._keyword_history:
                self._save(self._guilds, updated_history)
                self._keyword_history = updated_history
            return _copy_settings(DEFAULT_AUTOREPLY_SETTINGS)


class AutoReplyStatsStore:
    """Persist reply totals and per-user counts separately from settings."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "guilds"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or not isinstance(payload["guilds"], dict)
        ):
            raise ValueError(f"Invalid auto-reply statistics at {self.path}")

        for guild_id, stats in payload["guilds"].items():
            if (
                not isinstance(guild_id, str)
                or not guild_id.isdecimal()
                or int(guild_id) <= 0
                or not isinstance(stats, dict)
                or set(stats) != {"count", "users"}
                or type(stats["count"]) is not int
                or stats["count"] < 0
                or not isinstance(stats["users"], dict)
            ):
                raise ValueError(f"Invalid auto-reply statistics for guild {guild_id}")
            users = stats["users"]
            if any(
                not isinstance(user_id, str)
                or not user_id.isdecimal()
                or int(user_id) <= 0
                or type(count) is not int
                or count < 1
                for user_id, count in users.items()
            ) or sum(users.values()) != stats["count"]:
                raise ValueError(f"Invalid auto-reply user statistics for guild {guild_id}")
            self._guilds[guild_id] = {
                "count": stats["count"],
                "users": dict(users),
            }

    def _save(self, guilds: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = file.name
                json.dump(
                    {
                        "schema_version": 1,
                        "guilds": {
                            guild_id: guilds[guild_id]
                            for guild_id in sorted(guilds, key=int)
                        },
                    },
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                os.unlink(temporary_path)

    def get_stats(self, guild_id: int) -> dict:
        key = _guild_key(guild_id)
        with self._lock:
            stats = self._guilds.get(key, {"count": 0, "users": {}})
            return {
                "count": stats["count"],
                "users": dict(stats["users"]),
            }

    def record_reply(self, guild_id: int, user_id: int) -> dict:
        guild_key = _guild_key(guild_id)
        user_key = _guild_key(user_id)
        with self._lock:
            current = self._guilds.get(
                guild_key,
                {"count": 0, "users": {}},
            )
            stats = {
                "count": current["count"] + 1,
                "users": dict(current["users"]),
            }
            stats["users"][user_key] = stats["users"].get(user_key, 0) + 1
            updated = dict(self._guilds)
            updated[guild_key] = stats
            self._save(updated)
            self._guilds = updated
            return {
                "count": stats["count"],
                "users": dict(stats["users"]),
            }

    def clear_guild(self, guild_id: int) -> bool:
        key = _guild_key(guild_id)
        with self._lock:
            if key not in self._guilds:
                return False
            updated = dict(self._guilds)
            updated.pop(key)
            self._save(updated)
            self._guilds = updated
            return True


def contains_keyword(content: str, keywords: list[str]) -> bool:
    return bool(matching_keywords(content, keywords))


def matching_keywords(content: str, keywords: list[str]) -> list[str]:
    """Return configured phrases that match using the live whole-term rules."""
    text = _normalized_text(content)
    matches = []
    for keyword in keywords:
        normalized_keyword = _normalized_text(keyword)
        pattern = rf"(?<!\w){re.escape(normalized_keyword)}(?!\w)"
        if re.search(pattern, text, flags=re.UNICODE):
            matches.append(keyword)
    return matches


def _is_prefix_command(content: str, command_prefix) -> bool:
    if isinstance(command_prefix, str):
        prefixes = [command_prefix]
    elif isinstance(command_prefix, (list, tuple)):
        prefixes = command_prefix
    else:
        return False
    stripped = content.lstrip()
    return any(
        isinstance(prefix, str) and prefix and stripped.startswith(prefix)
        for prefix in prefixes
    )


async def get_server_status(
    session: aiohttp.ClientSession,
    address: str,
    port: int,
) -> dict | None:
    """Query the fixed Minecraft status endpoint; never fetch a user-supplied URL."""
    host = address.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    target = quote(f"{host}:{port}", safe="[]:.-")
    url = f"{_SERVER_STATUS_URL}{target}"
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(
                total=_SERVER_STATUS_TIMEOUT_SECONDS,
            ),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        UnicodeError,
        ValueError,
    ):
        logger.info("Minecraft status lookup failed for %s:%s.", address, port)
        return None

    if not isinstance(data, dict) or type(data.get("online")) is not bool:
        return None
    result = {"online": data["online"]}
    if not data["online"]:
        return result

    players = data.get("players")
    if not isinstance(players, dict):
        players = {}

    def nonnegative_int(value):
        if type(value) is not int or value < 0:
            return 0
        return min(value, 1_000_000_000)

    result["players_online"] = nonnegative_int(players.get("online"))
    result["players_max"] = nonnegative_int(players.get("max"))
    version = data.get("version")
    result["version"] = (
        discord.utils.escape_markdown(
            version.replace("@", "@\u200b")[:80]
        )
        if isinstance(version, str) and version.strip()
        else ""
    )
    return result


class AutoReplyView(discord.ui.View):
    """Offer private copy replies and an optional direct Discord invite link."""

    def __init__(
        self,
        settings: dict | None = None,
        *,
        settings_store: AutoReplyStore | None = None,
    ):
        super().__init__(timeout=None)
        self.settings = settings
        self.settings_store = settings_store
        self._add_copy_button("📋 نسخ Java IP", "copy_java")
        self._add_copy_button("📋 نسخ Bedrock", "copy_bedrock")
        if settings is not None and settings["discord_link"]:
            self.add_item(
                discord.ui.Button(
                    label="💬 ديسكورد",
                    style=discord.ButtonStyle.link,
                    url=settings["discord_link"],
                    emoji="🔗",
                )
            )

    @staticmethod
    def _copy_value(settings: dict, kind: str) -> str:
        if kind == "copy_java":
            value = settings["java_ip"]
            if settings["java_port"] != 25565:
                value += f":{settings['java_port']}"
            return value
        return f"{settings['bedrock_ip']}\nPort: {settings['bedrock_port']}"

    def _add_copy_button(self, label: str, custom_id: str) -> None:
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"autoreply:{custom_id}",
        )

        async def copy_value(interaction: discord.Interaction) -> None:
            settings = self.settings
            if settings is None:
                if self.settings_store is None or interaction.guild_id is None:
                    await interaction.response.send_message(
                        "لا تتوفر إعدادات السيرفر لهذا الزر.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                settings = self.settings_store.get_settings(
                    interaction.guild_id
                )
            value = self._copy_value(settings, custom_id)
            await interaction.response.send_message(
                f"```{value}```\n📋 انسخه والصقه في Minecraft.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        button.callback = copy_value
        self.add_item(button)


class AutoReplyHandler:
    """Send one static connection-info reply per guild/channel cooldown."""

    def __init__(
        self,
        store: AutoReplyStore,
        bot,
        stats_store: AutoReplyStatsStore | None = None,
        session_provider=None,
    ):
        self.store = store
        self.bot = bot
        self.stats_store = stats_store
        self.session_provider = session_provider
        self._last_reply_at: dict[tuple[int, int], float] = {}
        self._status_cache: dict[tuple[str, int], tuple[float, dict | None]] = {}
        self._status_lock = asyncio.Lock()

    async def _get_status(self, settings: dict) -> dict | None:
        if self.session_provider is None:
            return None
        cache_key = (settings["java_ip"], settings["java_port"])
        now = time.monotonic()
        async with self._status_lock:
            cached = self._status_cache.get(cache_key)
            if (
                cached is not None
                and now - cached[0] < _SERVER_STATUS_CACHE_SECONDS
            ):
                return dict(cached[1]) if cached[1] is not None else None

            session = self.session_provider()
            if session is None or session.closed:
                return None
            status = await get_server_status(
                session,
                settings["java_ip"],
                settings["java_port"],
            )
            self._status_cache[cache_key] = (time.monotonic(), status)
            return dict(status) if status is not None else None

    @staticmethod
    def _make_embed(
        settings: dict,
        bot_user,
        status: dict | None = None,
    ) -> discord.Embed:
        server_name = (
            settings["server_name"]
            .replace("@", "@\u200b")
            .replace("\r", " ")
            .replace("\n", " ")
        )
        embed = discord.Embed(
            title=f"🌐 IP سيرفر {server_name}",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        if settings["show_status"]:
            if status is None:
                status_text = "⚪ تعذر التحقق مؤقتًا"
            elif status["online"]:
                version = status.get("version")
                status_text = "🟢 أونلاين"
                if version:
                    status_text += f" • `{version}`"
            else:
                status_text = "🔴 أوفلاين"
            embed.add_field(
                name="📊 حالة السيرفر",
                value=status_text,
                inline=False,
            )
        java_value = f"`{settings['java_ip']}`"
        if settings["java_port"] != 25565:
            java_value += f"\n🔌 Port: `{settings['java_port']}`"
        embed.add_field(
            name="🎮 Java",
            value=java_value,
            inline=False,
        )
        embed.add_field(
            name="🛏️ Bedrock",
            value=(
                f"📡 IP: `{settings['bedrock_ip']}`\n"
                f"🔌 Port: `{settings['bedrock_port']}`"
            ),
            inline=False,
        )
        if settings["show_players"] and status and status.get("online"):
            players_online = status.get("players_online", 0)
            players_max = status.get("players_max", 0)
            if players_max > 0:
                filled = min(
                    10,
                    int(10 * min(players_online, players_max) / players_max),
                )
                player_text = (
                    f"`{'█' * filled}{'░' * (10 - filled)}` "
                    f"**{players_online}**/{players_max}"
                )
            else:
                player_text = f"**{players_online}** لاعب"
            embed.add_field(
                name="👥 اللاعبين",
                value=player_text,
                inline=False,
            )
        if settings["discord_link"]:
            embed.add_field(
                name="💬 ديسكورد",
                value=f"[اضغط للانضمام]({settings['discord_link']})",
                inline=False,
            )
        embed.set_footer(text=settings["footer_text"])
        if settings["server_icon"]:
            embed.set_thumbnail(url=settings["server_icon"])
        elif bot_user is not None:
            avatar = getattr(bot_user, "display_avatar", None)
            avatar_url = getattr(avatar, "url", None)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
        return embed

    async def handle(self, message: discord.Message) -> bool:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        content = getattr(message, "content", "")
        if (
            guild is None
            or author is None
            or getattr(author, "bot", False)
            or not isinstance(content, str)
            or not content.strip()
            or _is_prefix_command(content, self.bot.command_prefix)
        ):
            return False

        bot_user = getattr(self.bot, "user", None)
        reference = getattr(message, "reference", None)
        referenced_message = getattr(reference, "resolved", None)
        referenced_author = getattr(referenced_message, "author", None)
        if (
            bot_user is not None
            and getattr(referenced_author, "id", None) == bot_user.id
        ):
            return False

        settings = self.store.get_settings(guild.id)
        if not settings["enabled"] or not contains_keyword(
            content,
            settings["keywords"],
        ):
            return False

        # Treat matching questions as handled even during cooldown, so a
        # mention of the bot does not also trigger a second AI reply.
        cooldown_key = (guild.id, message.channel.id)
        now = time.monotonic()
        last_reply = self._last_reply_at.get(cooldown_key)
        if (
            last_reply is not None
            and now - last_reply < settings["cooldown_seconds"]
        ):
            return True
        self._last_reply_at[cooldown_key] = now

        view = AutoReplyView(settings) if settings["show_buttons"] else None
        if settings["use_custom_message"]:
            custom_message = settings["custom_message"]
            replacements = {
                "java_ip": settings["java_ip"],
                "java_port": str(settings["java_port"]),
                "bedrock_ip": settings["bedrock_ip"],
                "bedrock_port": str(settings["bedrock_port"]),
                "server_name": settings["server_name"],
                "user": getattr(author, "mention", f"<@{author.id}>"),
            }
            rendered_message = _TEMPLATE_TOKEN.sub(
                lambda match: replacements.get(match.group(1), match.group(0)),
                custom_message,
            )
            rendered_message = rendered_message[:2_000]
            custom_mentions = (
                discord.AllowedMentions(
                    users=[author],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                )
                if "{user}" in custom_message
                else discord.AllowedMentions.none()
            )
            try:
                await message.reply(
                    rendered_message,
                    view=view,
                    mention_author=False,
                    allowed_mentions=custom_mentions,
                )
            except discord.HTTPException:
                logger.exception(
                    "Could not send an auto-reply in guild %s, channel %s.",
                    guild.id,
                    message.channel.id,
                )
                return True
            except Exception:
                logger.exception(
                    "Unexpected error sending an auto-reply in guild %s, channel %s.",
                    guild.id,
                    message.channel.id,
                )
                return True
            self._record_reply(guild.id, author.id)
            return True

        status = None
        if settings["show_status"] or settings["show_players"]:
            try:
                status = await self._get_status(settings)
            except Exception:
                logger.exception(
                    "Unexpected error checking the Minecraft server in guild %s.",
                    guild.id,
                )

        try:
            await message.reply(
                embed=self._make_embed(settings, bot_user, status),
                view=view,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not send an auto-reply in guild %s, channel %s.",
                guild.id,
                message.channel.id,
            )
            return True
        except Exception:
            logger.exception(
                "Unexpected error sending an auto-reply in guild %s, channel %s.",
                guild.id,
                message.channel.id,
            )
            return True
        self._record_reply(guild.id, author.id)
        return True

    def _record_reply(self, guild_id: int, user_id: int) -> None:
        if self.stats_store is None:
            return
        try:
            self.stats_store.record_reply(guild_id, user_id)
        except Exception:
            logger.exception(
                "Could not save auto-reply statistics for guild %s.",
                guild_id,
            )


def _settings_embed(guild_name: str, settings: dict) -> discord.Embed:
    embed = discord.Embed(
        title="💬 إعدادات الرد التلقائي عن IP",
        description=f"السيرفر: {guild_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="الحالة",
        value="✅ مفعّل" if settings["enabled"] else "❌ معطّل",
        inline=True,
    )
    embed.add_field(
        name="اسم السيرفر",
        value=settings["server_name"],
        inline=True,
    )
    embed.add_field(
        name="فترة الانتظار",
        value=f"{settings['cooldown_seconds']} ثانية لكل قناة",
        inline=True,
    )
    java_value = f"IP: `{settings['java_ip']}`\nPort: `{settings['java_port']}`"
    bedrock_value = (
        f"IP: `{settings['bedrock_ip']}`\nPort: `{settings['bedrock_port']}`"
    )
    embed.add_field(name="Java", value=java_value, inline=False)
    embed.add_field(name="Bedrock", value=bedrock_value, inline=False)
    keyword_preview = "\n".join(
        f"• {keyword}" for keyword in settings["keywords"]
    ) or "لا توجد عبارات"
    if len(keyword_preview) > 1_000:
        keyword_preview = keyword_preview[:997] + "..."
    embed.add_field(
        name=f"عبارات التفعيل ({len(settings['keywords'])})",
        value=keyword_preview,
        inline=False,
    )
    embed.add_field(
        name="رابط Discord",
        value=settings["discord_link"] or "غير مضبوط",
        inline=False,
    )
    embed.add_field(
        name="نوع الرد",
        value="📝 مخصص" if settings["use_custom_message"] else "📋 تلقائي",
        inline=True,
    )
    embed.add_field(
        name="الحالة واللاعبون",
        value=(
            f"الحالة: {'✅' if settings['show_status'] else '❌'}\n"
            f"اللاعبون: {'✅' if settings['show_players'] else '❌'}"
        ),
        inline=True,
    )
    embed.add_field(
        name="الأزرار",
        value="✅ مفعّلة" if settings["show_buttons"] else "❌ معطّلة",
        inline=True,
    )
    if settings["custom_message"]:
        embed.add_field(
            name="الرسالة المخصصة",
            value=settings["custom_message"][:1_024],
            inline=False,
        )
    if settings["server_icon"]:
        embed.add_field(
            name="صورة السيرفر",
            value=settings["server_icon"],
            inline=False,
        )
        embed.set_thumbnail(url=settings["server_icon"])
    embed.set_footer(text=settings["footer_text"])
    return embed


def _is_auto_reply_admin(user, developer_id: int) -> bool:
    if getattr(user, "id", None) == developer_id:
        return True
    permissions = getattr(user, "guild_permissions", None)
    return bool(
        getattr(permissions, "administrator", False)
        or getattr(permissions, "manage_guild", False)
    )


class RestoreDefaultKeywordsConfirmationView(discord.ui.View):
    """Confirm replacing a guild's custom IP-trigger phrases."""

    def __init__(
        self,
        store: AutoReplyStore,
        guild_id: int,
        requester_id: int,
        developer_id: int,
    ):
        super().__init__(timeout=_AUTO_REPLY_CONFIRMATION_TIMEOUT_SECONDS)
        self.store = store
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.developer_id = developer_id
        self.confirmation_message = None
        self._state = "pending"

    async def _check_requester(
        self,
        interaction: discord.Interaction,
        *,
        require_admin: bool,
    ) -> bool:
        if getattr(interaction, "guild_id", None) != self.guild_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح داخل السيرفر الذي بدأ العملية فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        requester_id = getattr(getattr(interaction, "user", None), "id", None)
        if requester_id != self.requester_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح للإداري الذي بدأ العملية فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if self._state != "pending":
            await interaction.response.send_message(
                "انتهى هذا التأكيد بالفعل؛ لم يتم تطبيق تغيير آخر.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if require_admin and not _is_auto_reply_admin(
            interaction.user,
            self.developer_id,
        ):
            self._state = "permission_revoked"
            self._disable_controls()
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "لم تعد تملك صلاحية إدارة هذا السيرفر؛ "
                    "لم يتم تغيير عبارات التفعيل."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="تأكيد الاستعادة",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not await self._check_requester(interaction, require_admin=True):
            return

        self._state = "confirmed"
        self._disable_controls()
        self.stop()
        try:
            self.store.restore_default_keywords(
                self.guild_id,
                actor_id=self.requester_id,
            )
        except (OSError, ValueError):
            logger.exception(
                "Could not restore default auto-reply keywords for guild %s.",
                self.guild_id,
            )
            content = "تعذر استعادة عبارات التفعيل؛ لم يتم تطبيق التغيير."
        else:
            content = "تمت استعادة عبارات التفعيل الافتراضية."

        await interaction.response.edit_message(
            content=content,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="إلغاء",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not await self._check_requester(interaction, require_admin=False):
            return

        self._state = "cancelled"
        self._disable_controls()
        self.stop()
        await interaction.response.edit_message(
            content=(
                "تم الإلغاء؛ لم يتم تغيير عبارات التفعيل أو بقية الإعدادات."
            ),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        if self._state != "pending":
            return
        self._state = "expired"
        self._disable_controls()
        if self.confirmation_message is None:
            return
        try:
            await self.confirmation_message.edit(
                content=(
                    "انتهت مهلة التأكيد؛ لم يتم تغيير عبارات التفعيل "
                    "أو بقية الإعدادات."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not mark auto-reply keyword confirmation expired."
            )


def setup_auto_reply_commands(
    bot,
    store: AutoReplyStore,
    developer_id: int,
    stats_store: AutoReplyStatsStore | None = None,
) -> None:
    if getattr(bot, "_auto_reply_command_registered", False):
        return

    actions = [
        app_commands.Choice(name="✅ تفعيل", value="enable"),
        app_commands.Choice(name="❌ تعطيل", value="disable"),
        app_commands.Choice(name="📝 تعيين رسالة مخصصة", value="set"),
        app_commands.Choice(name="📋 استخدام الرد الافتراضي", value="set_default"),
        app_commands.Choice(name="🌐 تغيير IP الجافا", value="set_java_ip"),
        app_commands.Choice(name="🔌 تغيير بورت الجافا", value="set_java_port"),
        app_commands.Choice(name="📡 تغيير IP البيدروك", value="set_bedrock_ip"),
        app_commands.Choice(name="🔌 تغيير بورت البيدروك", value="set_bedrock_port"),
        app_commands.Choice(name="📛 تغيير اسم السيرفر", value="set_name"),
        app_commands.Choice(name="🔗 تغيير رابط ديسكورد", value="set_discord"),
        app_commands.Choice(name="⏱️ تغيير فترة الانتظار", value="set_cooldown"),
        app_commands.Choice(name="🖼️ تغيير صورة السيرفر", value="set_icon"),
        app_commands.Choice(name="📊 عرض حالة السيرفر", value="set_status"),
        app_commands.Choice(name="👥 عرض عدد اللاعبين", value="set_players"),
        app_commands.Choice(name="🔘 عرض الأزرار", value="set_buttons"),
        app_commands.Choice(name="➕ إضافة عبارة تفعيل", value="keyword_add"),
        app_commands.Choice(name="➖ حذف عبارة تفعيل", value="keyword_remove"),
        app_commands.Choice(name="📋 عرض عبارات التفعيل", value="show_keywords"),
        app_commands.Choice(name="📜 سجل عبارات التفعيل", value="keyword_history"),
        app_commands.Choice(name="🧪 معاينة عبارات التفعيل", value="preview_keywords"),
        app_commands.Choice(
            name="🔄 استعادة عبارات التفعيل الافتراضية",
            value="restore_default_keywords",
        ),
        app_commands.Choice(name="👁️ عرض الإعدادات", value="show"),
        app_commands.Choice(name="🔄 إعادة الإعدادات للافتراضي", value="reset"),
        app_commands.Choice(name="📈 عرض الإحصائيات", value="stats"),
    ]

    @bot.tree.command(
        name="autoreply",
        description="إعداد الرد التلقائي عن IP وPort في هذا السيرفر",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        action="اختر العملية",
        value="القيمة الجديدة أو نص التجربة أو رقم صفحة سجل العبارات",
    )
    @app_commands.choices(action=actions)
    async def autoreply(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        value: str | None = None,
    ) -> None:
        if interaction.guild is None or interaction.guild_id is None:
            await interaction.response.send_message(
                "استخدم هذا الأمر داخل السيرفر.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if not _is_auto_reply_admin(interaction.user, developer_id):
            await interaction.response.send_message(
                "هذا الأمر لإداريي السيرفر فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        selected = action.value
        if selected == "show":
            settings = store.get_settings(interaction.guild_id)
            await interaction.response.send_message(
                embed=_settings_embed(interaction.guild.name, settings),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if selected == "show_keywords":
            keywords = store.get_settings(interaction.guild_id)["keywords"]
            description = (
                "\n".join(f"• {keyword}" for keyword in keywords)
                if keywords
                else "لا توجد عبارات تفعيل."
            )
            await interaction.response.send_message(
                f"عبارات تفعيل الرد التلقائي:\n{description}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
            return

        if selected == "keyword_history":
            raw_page = value.strip() if isinstance(value, str) else ""
            if raw_page and not raw_page.isdecimal():
                await interaction.response.send_message(
                    "أدخل رقم صفحة صحيحًا لسجل العبارات.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            page = int(raw_page) if raw_page else 1
            history = store.get_keyword_history(interaction.guild_id)
            if not history:
                await interaction.response.send_message(
                    "لا توجد تغييرات محفوظة على عبارات التفعيل.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            page_count = (
                len(history) + _KEYWORD_HISTORY_PAGE_SIZE - 1
            ) // _KEYWORD_HISTORY_PAGE_SIZE
            if page < 1 or page > page_count:
                await interaction.response.send_message(
                    f"رقم الصفحة يجب أن يكون بين 1 و{page_count}.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            newest_first = list(reversed(history))
            start = (page - 1) * _KEYWORD_HISTORY_PAGE_SIZE
            events = newest_first[start : start + _KEYWORD_HISTORY_PAGE_SIZE]
            action_labels = {
                "added": "أُضيفت",
                "removed": "حُذفت",
                "restored_defaults": "استُعيدت العبارات الافتراضية",
            }
            lines = []
            for event in events:
                action_text = action_labels[event["action"]]
                phrase = event["phrase"]
                if phrase is not None:
                    safe_phrase = discord.utils.escape_markdown(
                        phrase.replace("\r", " ").replace("\n", " ")
                    )
                    action_text += f" «{safe_phrase}»"
                lines.append(
                    f"• {action_text} — <@{event['actor_id']}> "
                    f"• <t:{event['timestamp']}:f>"
                )
            await interaction.response.send_message(
                f"سجل عبارات التفعيل — صفحة {page}/{page_count}:\n"
                + "\n".join(lines),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if selected == "preview_keywords":
            sample = value if isinstance(value, str) else ""
            if not sample.strip():
                response = "أدخل نصًا تجريبيًا في حقل value لمعاينة عبارات التفعيل."
            elif len(sample) > _MAX_KEYWORD_PREVIEW_LENGTH:
                response = (
                    "يجب ألا يتجاوز النص التجريبي "
                    f"{_MAX_KEYWORD_PREVIEW_LENGTH} حرف."
                )
            else:
                keywords = store.get_settings(interaction.guild_id)["keywords"]
                matches = matching_keywords(sample, keywords)
                if matches:
                    formatted_matches = "\n".join(
                        f"• {discord.utils.escape_markdown(keyword.replace(chr(10), ' ').replace(chr(13), ' '))}"
                        for keyword in matches
                    )
                    response = (
                        f"تطابقت عبارات التفعيل التالية:\n{formatted_matches}\n"
                        "هذه معاينة خاصة؛ لم يتم إرسال رد عام."
                    )
                else:
                    response = (
                        "لم تطابق أي عبارة من عبارات التفعيل المحفوظة في هذا السيرفر.\n"
                        "هذه معاينة خاصة؛ لم يتم إرسال رد عام."
                    )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
            return

        if selected == "stats":
            stats = (
                stats_store.get_stats(interaction.guild_id)
                if stats_store is not None
                else {"count": 0, "users": {}}
            )
            embed = discord.Embed(
                title="📊 إحصائيات الرد التلقائي",
                description=f"السيرفر: {interaction.guild.name}",
                color=discord.Color.gold(),
            )
            embed.add_field(
                name="عدد الردود",
                value=f"`{stats['count']}`",
                inline=True,
            )
            embed.add_field(
                name="عدد الأعضاء",
                value=f"`{len(stats['users'])}`",
                inline=True,
            )
            if stats["users"]:
                top_users = sorted(
                    stats["users"].items(),
                    key=lambda item: (-item[1], int(item[0])),
                )[:5]
                lines = []
                for user_id, count in top_users:
                    member = interaction.guild.get_member(int(user_id))
                    display_name = (
                        discord.utils.escape_markdown(member.display_name)
                        if member is not None
                        else f"ID {user_id}"
                    )
                    lines.append(f"• {display_name}: `{count}`")
                embed.add_field(
                    name="الأكثر سؤالًا",
                    value="\n".join(lines),
                    inline=False,
                )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if selected == "restore_default_keywords":
            current_keywords = store.get_settings(interaction.guild_id)["keywords"]
            default_keywords = list(DEFAULT_AUTOREPLY_SETTINGS["keywords"])
            if current_keywords == default_keywords:
                await interaction.response.send_message(
                    "عبارات التفعيل الافتراضية مستخدمة بالفعل؛ لم تتغير الإعدادات.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            view = RestoreDefaultKeywordsConfirmationView(
                store,
                interaction.guild_id,
                interaction.user.id,
                developer_id,
            )
            await interaction.response.send_message(
                content=(
                    "سيؤدي تأكيد الاستعادة إلى استبدال قائمة عبارات التفعيل "
                    f"الحالية ({len(current_keywords)} عبارة) بالعبارات "
                    "الافتراضية المدمجة. لن تتغير بقية إعدادات الرد التلقائي. "
                    "لم يتم حفظ أي تغيير بعد."
                ),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.confirmation_message = await interaction.original_response()
            return

        if selected == "reset":
            try:
                store.reset(
                    interaction.guild_id,
                    actor_id=interaction.user.id,
                )
            except (OSError, ValueError):
                logger.exception(
                    "Could not reset auto-reply settings for guild %s.",
                    interaction.guild_id,
                )
                response = "تعذر إعادة الإعدادات؛ لم يتم تطبيق التغيير."
            else:
                response = "تمت إعادة إعدادات الرد التلقائي إلى القيم الافتراضية."
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if selected not in {choice.value for choice in actions}:
            await interaction.response.send_message(
                "العملية غير معروفة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        keyword_history_action = None
        keyword_history_phrase = None
        if selected in {"enable", "disable"}:
            changes = {"enabled": selected == "enable"}
            success = (
                "تم تفعيل الرد التلقائي عن IP."
                if selected == "enable"
                else "تم تعطيل الرد التلقائي عن IP."
            )
        elif selected == "set_default":
            changes = {"use_custom_message": False}
            success = "تم تفعيل الرد الافتراضي."
        else:
            raw_value = value.strip() if isinstance(value, str) else ""
            if selected == "set" and not raw_value:
                settings = store.get_settings(interaction.guild_id)
                embed = discord.Embed(
                    title="📝 الرسالة المخصصة",
                    description=(
                        "أدخل نص الرسالة في حقل value. المتغيرات المتاحة: "
                        "`{java_ip}`, `{java_port}`, `{bedrock_ip}`, "
                        "`{bedrock_port}`, `{server_name}`, `{user}`."
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(
                    name="الرسالة الحالية",
                    value=settings["custom_message"] or "غير مضبوطة",
                    inline=False,
                )
                await interaction.response.send_message(
                    embed=embed,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if selected in {"keyword_add", "keyword_remove"} and not raw_value:
                response = (
                    "أدخل عبارة التفعيل في حقل value."
                    if selected == "keyword_add"
                    else "أدخل عبارة التفعيل التي تريد حذفها في حقل value."
                )
                await interaction.response.send_message(
                    response,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not raw_value:
                await interaction.response.send_message(
                    "أدخل القيمة المطلوبة في حقل value.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            try:
                if selected == "keyword_add":
                    if len(raw_value) > 80:
                        raise ValueError("يجب ألا تتجاوز عبارة التفعيل 80 حرفًا.")
                    settings = store.get_settings(interaction.guild_id)
                    normalized = _normalized_text(raw_value)
                    if any(
                        _normalized_text(keyword) == normalized
                        for keyword in settings["keywords"]
                    ):
                        raise ValueError("عبارة التفعيل موجودة بالفعل.")
                    if len(settings["keywords"]) >= _MAX_AUTO_REPLY_KEYWORDS:
                        raise ValueError(
                            f"لا يمكن إضافة أكثر من {_MAX_AUTO_REPLY_KEYWORDS} عبارة تفعيل."
                        )
                    changes = {
                        "keywords": [*settings["keywords"], raw_value],
                    }
                    keyword_history_action = "added"
                    keyword_history_phrase = raw_value
                    success = "تمت إضافة عبارة التفعيل."
                elif selected == "keyword_remove":
                    settings = store.get_settings(interaction.guild_id)
                    normalized = _normalized_text(raw_value)
                    removed_keyword = next(
                        (
                            keyword
                            for keyword in settings["keywords"]
                            if _normalized_text(keyword) == normalized
                        ),
                        None,
                    )
                    remaining_keywords = [
                        keyword
                        for keyword in settings["keywords"]
                        if _normalized_text(keyword) != normalized
                    ]
                    if removed_keyword is None:
                        raise ValueError("لم يتم العثور على عبارة التفعيل.")
                    changes = {"keywords": remaining_keywords}
                    keyword_history_action = "removed"
                    keyword_history_phrase = removed_keyword
                    success = "تم حذف عبارة التفعيل."
                elif selected == "set":
                    changes = {
                        "custom_message": raw_value,
                        "use_custom_message": True,
                    }
                    success = "تم حفظ الرسالة المخصصة وتفعيلها."
                elif selected in {"set_java_ip", "set_bedrock_ip"}:
                    changes = {
                        selected.removeprefix("set_"): _normalize_server_address(
                            raw_value
                        )
                    }
                    success = (
                        "تم تحديث عنوان Java."
                        if selected == "set_java_ip"
                        else "تم تحديث عنوان Bedrock."
                    )
                elif selected in {"set_java_port", "set_bedrock_port"}:
                    if not raw_value.isdecimal():
                        raise ValueError("يجب أن يكون المنفذ رقمًا بين 1 و65535.")
                    port = int(raw_value)
                    if not 1 <= port <= 65_535:
                        raise ValueError("يجب أن يكون المنفذ بين 1 و65535.")
                    changes = {selected.removeprefix("set_"): port}
                    success = (
                        "تم تحديث منفذ Java."
                        if selected == "set_java_port"
                        else "تم تحديث منفذ Bedrock."
                    )
                elif selected == "set_name":
                    if (
                        len(raw_value) > 80
                        or "\n" in raw_value
                        or "\r" in raw_value
                    ):
                        raise ValueError(
                            "اسم السيرفر يجب ألا يتجاوز 80 حرفًا أو يحتوي أسطرًا جديدة."
                        )
                    changes = {"server_name": raw_value}
                    success = "تم تحديث اسم السيرفر."
                elif selected == "set_discord":
                    if raw_value.casefold() in {"clear", "none", "مسح"}:
                        changes = {"discord_link": ""}
                        success = "تم حذف رابط دعوة Discord."
                    else:
                        changes = {
                            "discord_link": normalize_invite_target(raw_value)
                        }
                        success = "تم تحديث رابط دعوة Discord."
                elif selected == "set_cooldown":
                    if not raw_value.isdecimal():
                        raise ValueError("أدخل فترة انتظار من 5 إلى 86400 ثانية.")
                    cooldown = int(raw_value)
                    if not _MIN_COOLDOWN_SECONDS <= cooldown <= _MAX_COOLDOWN_SECONDS:
                        raise ValueError("فترة الانتظار يجب أن تكون بين 5 و86400 ثانية.")
                    changes = {"cooldown_seconds": cooldown}
                    success = "تم تحديث فترة الانتظار."
                elif selected == "set_icon":
                    icon_url = (
                        None
                        if raw_value.casefold() in {"clear", "none", "مسح"}
                        else _normalize_image_url(raw_value)
                    )
                    changes = {"server_icon": icon_url}
                    success = (
                        "تم حذف صورة السيرفر."
                        if icon_url is None
                        else "تم تحديث صورة السيرفر."
                    )
                elif selected in {"set_status", "set_players", "set_buttons"}:
                    normalized_value = raw_value.casefold()
                    if normalized_value not in {
                        "on",
                        "off",
                        "true",
                        "false",
                        "yes",
                        "no",
                        "تشغيل",
                        "إيقاف",
                        "ايقاف",
                    }:
                        raise ValueError(
                            "استخدم on/off أو تشغيل/إيقاف لهذا الإعداد."
                        )
                    enabled = normalized_value in {
                        "on",
                        "true",
                        "yes",
                        "تشغيل",
                    }
                    setting_key = {
                        "set_status": "show_status",
                        "set_players": "show_players",
                        "set_buttons": "show_buttons",
                    }[selected]
                    changes = {setting_key: enabled}
                    success = (
                        "تم تفعيل الإعداد."
                        if enabled
                        else "تم إيقاف الإعداد."
                    )
                else:
                    await interaction.response.send_message(
                        "العملية غير معروفة.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
            except ValueError as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        try:
            if keyword_history_action is None:
                store.update(interaction.guild_id, **changes)
            else:
                store.update_keywords(
                    interaction.guild_id,
                    changes["keywords"],
                    actor_id=interaction.user.id,
                    action=keyword_history_action,
                    phrase=keyword_history_phrase,
                )
        except (OSError, ValueError):
            logger.exception(
                "Could not update auto-reply settings for guild %s.",
                interaction.guild_id,
            )
            response = "تعذر حفظ الإعداد؛ لم يتم تطبيق التغيير."
        else:
            response = success
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot._auto_reply_command_registered = True
    bot.add_view(AutoReplyView(settings_store=store))