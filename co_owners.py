"""Persistent per-guild assistant-owner access."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _positive_id(value: int, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class CoOwnerStore:
    """Store the Discord IDs of assistant owners separately for each guild."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.backup_dir = self.path.parent / "backup"
        self._lock = threading.RLock()
        self._guilds: dict[str, list[int]] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as source:
                self._guilds = self._validate(json.load(source))

    @staticmethod
    def _validate(payload) -> dict[str, list[int]]:
        if not isinstance(payload, dict) or set(payload) != {"guilds"}:
            raise ValueError("Invalid assistant-owner data: expected a guild map.")
        guilds = payload["guilds"]
        if not isinstance(guilds, dict):
            raise ValueError("Invalid assistant-owner data: guilds must be an object.")
        checked: dict[str, list[int]] = {}
        for guild_id, user_ids in guilds.items():
            if (
                not isinstance(guild_id, str)
                or not guild_id.isdecimal()
                or int(guild_id) <= 0
            ):
                raise ValueError("Invalid guild ID in assistant-owner data.")
            if (
                not isinstance(user_ids, list)
                or any(type(user_id) is not int or user_id <= 0 for user_id in user_ids)
                or len(user_ids) != len(set(user_ids))
            ):
                raise ValueError(
                    f"Invalid assistant-owner IDs for guild {guild_id}."
                )
            checked[guild_id] = list(user_ids)
        return checked

    def _save(self, guilds: dict[str, list[int]]) -> None:
        _atomic_write(self.path, {"guilds": guilds})
        self._guilds = guilds

    def get(self, guild_id: int) -> list[int]:
        key = str(_positive_id(guild_id, "guild ID"))
        with self._lock:
            return list(self._guilds.get(key, []))

    def is_co_owner(self, guild_id: int, user_id: int) -> bool:
        key = str(_positive_id(guild_id, "guild ID"))
        uid = _positive_id(user_id, "user ID")
        with self._lock:
            return uid in self._guilds.get(key, [])

    def add(self, guild_id: int, user_id: int) -> bool:
        key = str(_positive_id(guild_id, "guild ID"))
        uid = _positive_id(user_id, "user ID")
        with self._lock:
            current = self._guilds.get(key, [])
            if uid in current:
                return False
            updated = copy.deepcopy(self._guilds)
            updated.setdefault(key, []).append(uid)
            self._save(updated)
            return True

    def remove(self, guild_id: int, user_id: int) -> bool:
        key = str(_positive_id(guild_id, "guild ID"))
        uid = _positive_id(user_id, "user ID")
        with self._lock:
            current = self._guilds.get(key, [])
            if uid not in current:
                return False
            updated = copy.deepcopy(self._guilds)
            updated[key].remove(uid)
            if not updated[key]:
                updated.pop(key)
            self._save(updated)
            return True

    def reset_with_backup(self, guild_id: int) -> tuple[int, str | None]:
        """Clear one guild only after a private, full-state backup succeeds."""
        key = str(_positive_id(guild_id, "guild ID"))
        with self._lock:
            current = self._guilds.get(key, [])
            if not current:
                return 0, None

            self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.backup_dir.is_symlink():
                raise OSError("assistant-owner backup directory must not be a symlink")
            self.backup_dir.chmod(0o700)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.backup_dir / (
                f"co_owners_{stamp}_{uuid.uuid4().hex}.json"
            )
            _atomic_write(backup, {"guilds": copy.deepcopy(self._guilds)})

            updated = copy.deepcopy(self._guilds)
            count = len(updated.pop(key))
            self._save(updated)
            return count, backup.name


def is_owner_or_co_owner(
    user,
    guild,
    developer_id: int,
    co_owner_store: CoOwnerStore | None,
) -> bool:
    """Check the bot developer, a guild owner, or that guild's assistant owner."""
    if guild is None or user is None:
        return False
    user_id = getattr(user, "id", None)
    if type(user_id) is not int or user_id <= 0:
        return False
    if user_id in {int(developer_id), getattr(guild, "owner_id", None)}:
        return True
    return bool(
        co_owner_store is not None
        and co_owner_store.is_co_owner(guild.id, user_id)
    )


def can_manage_server_settings(
    user,
    guild,
    developer_id: int,
    co_owner_store: CoOwnerStore | None,
) -> bool:
    """Allow existing Discord admins plus developer, guild owner, and co-owners."""
    if is_owner_or_co_owner(user, guild, developer_id, co_owner_store):
        return True
    permissions = getattr(user, "guild_permissions", None)
    return bool(getattr(permissions, "administrator", False))