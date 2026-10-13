"""Persistent maintenance mode for pausing member-facing bot replies."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands


logger = logging.getLogger(__name__)

DEFAULT_MAINTENANCE_MESSAGE = "🔧 البوت تحت الصيانة حاليًا. حاول مرة أخرى لاحقًا."
MAX_MAINTENANCE_MESSAGE_LENGTH = 500
MAINTENANCE_CHANGE_SOURCES = frozenset({"discord", "dashboard"})


class MaintenanceStore:
    """Persist maintenance settings and their change history."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._enabled = False
        self._message = DEFAULT_MAINTENANCE_MESSAGE
        self._history: list[dict] = []
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not load maintenance settings at {self.path}") from error

        if not isinstance(data, dict):
            raise ValueError(f"Invalid maintenance settings at {self.path}")

        schema_version = data.get("schema_version")
        if schema_version == 1:
            expected_keys = {"schema_version", "enabled", "message"}
        elif schema_version == 2:
            expected_keys = {"schema_version", "enabled", "message", "history"}
        else:
            raise ValueError(f"Invalid maintenance settings at {self.path}")

        if (
            set(data) != expected_keys
            or type(schema_version) is not int
            or type(data["enabled"]) is not bool
            or not isinstance(data["message"], str)
            or not data["message"].strip()
            or len(data["message"]) > MAX_MAINTENANCE_MESSAGE_LENGTH
        ):
            raise ValueError(f"Invalid maintenance settings at {self.path}")

        self._enabled = data["enabled"]
        self._message = data["message"].strip()
        self._history = (
            self._validate_history(data["history"])
            if schema_version == 2
            else []
        )

    @staticmethod
    def _validate_history(history: object) -> list[dict]:
        if not isinstance(history, list):
            raise ValueError("Invalid maintenance change history")

        validated = []
        for entry in history:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"enabled", "message", "source", "changed_at"}
                or type(entry["enabled"]) is not bool
                or not isinstance(entry["message"], str)
                or not entry["message"].strip()
                or len(entry["message"]) > MAX_MAINTENANCE_MESSAGE_LENGTH
                or not isinstance(entry["source"], str)
                or entry["source"] not in MAINTENANCE_CHANGE_SOURCES
                or not isinstance(entry["changed_at"], str)
            ):
                raise ValueError("Invalid maintenance change history")
            try:
                changed_at = datetime.fromisoformat(
                    entry["changed_at"].replace("Z", "+00:00")
                )
            except ValueError as error:
                raise ValueError("Invalid maintenance change history") from error
            if changed_at.tzinfo is None or changed_at.utcoffset() is None:
                raise ValueError("Invalid maintenance change history")

            validated.append(
                {
                    "enabled": entry["enabled"],
                    "message": entry["message"].strip(),
                    "source": entry["source"],
                    "changed_at": entry["changed_at"],
                }
            )
        return validated

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    @property
    def history(self) -> list[dict]:
        with self._lock:
            return [entry.copy() for entry in self._history]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "message": self._message,
                "history": [entry.copy() for entry in self._history],
            }

    def set_state(
        self,
        enabled: bool,
        message: str | None = None,
        *,
        source: str,
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("Maintenance enabled state must be a boolean")
        if not isinstance(source, str) or source not in MAINTENANCE_CHANGE_SOURCES:
            raise ValueError("Invalid maintenance change source")

        with self._lock:
            next_message = self._message
            if message is not None:
                if not isinstance(message, str):
                    raise ValueError("Maintenance message must be text")
                next_message = message.strip()
                if not next_message:
                    raise ValueError("Maintenance message cannot be empty")
                if len(next_message) > MAX_MAINTENANCE_MESSAGE_LENGTH:
                    raise ValueError(
                        f"Maintenance message cannot exceed "
                        f"{MAX_MAINTENANCE_MESSAGE_LENGTH} characters"
                    )

            if enabled == self._enabled and next_message == self._message:
                return

            history = [
                *self._history,
                {
                    "enabled": enabled,
                    "message": next_message,
                    "source": source,
                    "changed_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ]
            data = {
                "schema_version": 2,
                "enabled": enabled,
                "message": next_message,
                "history": history,
            }
            self._save(data)
            self._enabled = enabled
            self._message = next_message
            self._history = history

    def _save(self, data: dict) -> None:
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
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                os.unlink(temporary_path)


async def maintenance_interaction_check(
    interaction: discord.Interaction,
    store: MaintenanceStore,
    developer_id: int,
) -> bool:
    """Allow only the developer and the maintenance command during maintenance."""
    if not store.enabled or interaction.user.id == developer_id:
        return True

    data = getattr(interaction, "data", None)
    command_name = data.get("name") if isinstance(data, dict) else None
    if command_name == "maintenance":
        return True

    if getattr(interaction, "type", None) == discord.InteractionType.autocomplete:
        return False

    response = interaction.response
    if not response.is_done():
        try:
            await response.send_message(
                store.message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not send the maintenance notice.")
    return False


async def handle_maintenance_message(
    message,
    bot,
    store: MaintenanceStore,
    developer_id: int,
) -> bool:
    """Consume member messages during maintenance, notifying only on direct contact."""
    if not store.enabled or message.author.id == developer_id:
        return False

    is_direct_message = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user is not None and bot.user.mentioned_in(message)
    if is_direct_message or is_mentioned:
        try:
            await message.reply(
                store.message,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not reply with the maintenance notice.")
    return True


def setup_maintenance_commands(bot, store: MaintenanceStore, developer_id: int) -> None:
    if getattr(bot, "_maintenance_mode_setup", False):
        return

    previous_interaction_check = bot.tree.interaction_check

    async def interaction_check(interaction: discord.Interaction) -> bool:
        if not await previous_interaction_check(interaction):
            return False
        return await maintenance_interaction_check(
            interaction,
            store,
            developer_id,
        )

    bot.tree.interaction_check = interaction_check

    @bot.tree.command(
        name="maintenance",
        description="تشغيل وضع الصيانة أو إيقافه أو عرض حالته",
    )
    @app_commands.describe(
        action="اختيار حالة وضع الصيانة",
        message="رسالة الصيانة الاختيارية للأعضاء",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="تشغيل الصيانة", value="on"),
            app_commands.Choice(name="إيقاف الصيانة", value="off"),
            app_commands.Choice(name="عرض الحالة", value="status"),
        ]
    )
    async def maintenance_command(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        message: str | None = None,
    ) -> None:
        if interaction.user.id != developer_id:
            await interaction.response.send_message(
                "هذا الأمر متاح لمطور البوت فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value == "status":
            state = "مفعّل" if store.enabled else "متوقف"
            response = f"حالة وضع الصيانة: **{state}**"
            if store.enabled:
                response += f"\nرسالة الأعضاء: {store.message}"
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            store.set_state(
                action.value == "on",
                message if action.value == "on" else None,
                source="discord",
            )
        except (OSError, ValueError):
            logger.exception("Could not update maintenance mode.")
            await interaction.response.send_message(
                "تعذر حفظ إعداد الصيانة. تأكد أن الرسالة لا تتجاوز "
                f"{MAX_MAINTENANCE_MESSAGE_LENGTH} حرفًا.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value == "on":
            response = (
                "تم تفعيل وضع الصيانة. ستتوقف ردود المساعد وأوامر البوت للأعضاء "
                "حتى إيقافه، مع استمرار أدوات الحماية والإشراف."
            )
        else:
            response = "تم إيقاف وضع الصيانة؛ عاد البوت للعمل."
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot._maintenance_mode_setup = True