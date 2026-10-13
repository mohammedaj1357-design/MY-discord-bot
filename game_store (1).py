"""Game state stores. Mutations must be synchronous inside transaction() (no await).

The bot uses PostgresGameStore; GameStore remains for local tests and offline use.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


STARTING_MONEY = 5000


def validate_state(data, source):
    if not isinstance(data, dict):
        raise ValueError(f"Invalid game data at {source}: expected a JSON object")
    for key in (
        "users",
        "lottery",
        "cooldowns",
        "levels",
        "level_role_rewards",
        "level_announcement_settings",
        "level_notice_delivery_failures",
        "manual_xp_grants",
        "level_medals",
    ):
        if key in data and not isinstance(data[key], dict):
            raise ValueError(f"Invalid game data at {source}: {key} must be an object")
    for guild_id, rewards in data.get("level_role_rewards", {}).items():
        if not isinstance(rewards, dict):
            raise ValueError(
                f"Invalid game data at {source}: level role rewards for "
                f"guild {guild_id} must be an object"
            )
        for level, role_id in rewards.items():
            try:
                if int(level) <= 0 or int(role_id) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid game data at {source}: level role rewards must "
                    "use positive level and role IDs"
                ) from None
    for guild_id, settings in data.get("level_announcement_settings", {}).items():
        if not isinstance(settings, dict):
            raise ValueError(
                f"Invalid game data at {source}: level announcement settings "
                f"for guild {guild_id} must be an object"
            )
        mode = settings.get("mode", "same")
        if not isinstance(mode, str) or mode not in {"same", "channel", "dm", "off"}:
            raise ValueError(
                f"Invalid game data at {source}: unknown level announcement mode"
            )
        channel_id = settings.get("channel_id")
        if channel_id is not None and (
            isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or channel_id <= 0
        ):
            raise ValueError(
                f"Invalid game data at {source}: level announcement channel ID "
                "must be a positive integer"
            )
        if mode == "channel" and channel_id is None:
            raise ValueError(
                f"Invalid game data at {source}: custom announcement mode "
                "requires a channel ID"
            )
        if "mention_user" in settings and not isinstance(settings["mention_user"], bool):
            raise ValueError(
                f"Invalid game data at {source}: level mention_user must be boolean"
            )
        custom_message = settings.get("custom_message")
        if custom_message is not None and (
            not isinstance(custom_message, str)
            or not custom_message.strip()
            or len(custom_message) > 500
        ):
            raise ValueError(
                f"Invalid game data at {source}: level custom message must be "
                "a string of at most 500 characters"
            )
    if "tickets" in data.get("lottery", {}) and not isinstance(data["lottery"]["tickets"], dict):
        raise ValueError(f"Invalid game data at {source}: lottery tickets must be an object")
    result = copy.deepcopy(data)
    result.setdefault("users", {})
    result.setdefault("lottery", {})
    result["lottery"].setdefault("tickets", {})
    result["lottery"].setdefault("pot", 0)
    result.setdefault("cooldowns", {})
    result.setdefault("levels", {})
    result.setdefault("level_role_rewards", {})
    result.setdefault("level_announcement_settings", {})
    result.setdefault("level_notice_delivery_failures", {})
    result.setdefault("manual_xp_grants", {})
    result.setdefault("level_medals", {})
    return result


class GameStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        if self.path.exists():
            # Never silently replace corrupted player data with an empty game.
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        else:
            data = {}
        self._data = validate_state(data, self.path)

    @staticmethod
    def user(data, user_id):
        """Return a mutable player in a transaction, creating one if absent."""
        uid = str(user_id)
        if uid not in data["users"]:
            data["users"][uid] = {
                "money": STARTING_MONEY,
                "lands": [],
                "total_earned": 0,
                "created_at": datetime.now().isoformat(),
            }
        return data["users"][uid]

    def snapshot(self):
        """Return an isolated copy for reads without writing to disk."""
        with self._lock:
            return copy.deepcopy(self._data)

    @contextmanager
    def transaction(self):
        """Hold the RLock through mutation and persist or roll back as one unit."""
        with self._lock:
            before = copy.deepcopy(self._data)
            try:
                yield self._data
                if self._data != before:
                    self._save()
            except BaseException:
                self._data = before
                raise

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as file:
                temp_path = file.name
                json.dump(self._data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
        finally:
            if temp_path is not None:
                os.unlink(temp_path)


class PostgresGameStore(GameStore):
    """One serialized JSON document; SELECT FOR UPDATE guards all settlements.

    Every transaction reloads from the database, so different processes cannot
    settle using stale balances. No local-file fallback is allowed.
    """

    def __init__(self, path, database_url, *, require_existing=False):
        if not database_url:
            raise RuntimeError("DATABASE_URL is required for durable game storage")
        self.path = Path(path)  # Only used as the location for local admin exports.
        self.database_url = database_url
        self._lock = threading.RLock()
        import psycopg
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                # ═══════════════════════════════════════════════════
                # ═══ إنشاء الجدول لو ما موجود ═══
                # ═══════════════════════════════════════════════════
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS discord_game_state (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        state JSONB NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
                
                # ═══ اقرأ الحالة ═══
                cur.execute("SELECT state FROM discord_game_state WHERE id = 1")
                row = cur.fetchone()
                if row is None and require_existing:
                    raise RuntimeError(
                        "Production game is locked until legacy data is imported "
                        "or a new game is explicitly initialized."
                    )
                if row is None and self.path.exists():
                    raise RuntimeError(
                        "Unmigrated local game data exists. Stop the bot and import it "
                        "with migrate_game_data.py before enabling database storage."
                    )
                if row is not None:
                    validate_state(row[0], "discord_game_state")

    def snapshot(self):
        import psycopg
        with self._lock, psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM discord_game_state WHERE id = 1")
                row = cur.fetchone()
                return validate_state(row[0] if row else {}, "discord_game_state")

    @contextmanager
    def transaction(self):
        import psycopg
        with self._lock, psycopg.connect(self.database_url) as conn:
            # Insert a single sentinel row, then lock it for the entire read,
            # mutation, and write. Concurrent inserts block on the unique id.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO discord_game_state (id, state) VALUES (1, %s::jsonb) "
                    "ON CONFLICT (id) DO NOTHING",
                    (json.dumps(validate_state({}, "empty")),)
                )
                cur.execute("SELECT state FROM discord_game_state WHERE id = 1 FOR UPDATE")
                before = validate_state(cur.fetchone()[0], "discord_game_state")
                data = copy.deepcopy(before)
                yield data
                if data != before:
                    cur.execute(
                        "UPDATE discord_game_state SET state = %s::jsonb, updated_at = now() "
                        "WHERE id = 1",
                        (json.dumps(data, ensure_ascii=False),)
                    )