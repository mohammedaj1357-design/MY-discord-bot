"""Synchronous developer administration rules for the local game store."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from game_store import GameStore
from economy_commands import GameManager

MAX_ADMIN_AMOUNT = 1_000_000_000_000
MAX_BROADCAST_LENGTH = 1800


class AdminError(ValueError):
    """Safe, user-facing validation or persistence error."""


def nonnegative_amount(value, *, cap=MAX_ADMIN_AMOUNT):
    """Validate an integer amount without accepting bools or signed strings."""
    if isinstance(value, bool):
        raise AdminError(f"Enter a whole number from 0 to {cap:,}.")
    try:
        text = str(value)
        if not text.isascii() or not text.isdecimal():
            raise ValueError
        amount = int(text)
    except (TypeError, ValueError):
        raise AdminError(f"Enter a whole number from 0 to {cap:,}.") from None
    if amount > cap:
        raise AdminError(f"Amount must not exceed {cap:,}.")
    return amount


class AdminEngine:
    def __init__(self, store: GameStore):
        self.store = store
        self.backup_dir = store.path.parent / "backup"

    def create_backup(self, state=None):
        """Write a complete point-in-time store snapshot privately and atomically."""
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.backup_dir.is_symlink():
                raise OSError("backup directory must not be a symbolic link")
            self.backup_dir.chmod(0o700)
            if state is None:
                state = self.store.snapshot()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = self.backup_dir / f"backup_{stamp}_{uuid.uuid4().hex}.json"
            fd, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=self.backup_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(state, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, destination)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            return destination
        except (OSError, TypeError, ValueError):
            raise AdminError("Backup failed; no change was made.") from None

    def _apply(self, action, details, mutation):
        # Export the exact pre-mutation state while holding the database row lock.
        with self.store.transaction() as data:
            self.create_backup(state=data)
            result = mutation(data)
            logs = data.setdefault("admin_logs", [])
            if not isinstance(logs, list):
                raise AdminError("Admin audit log is invalid; operation aborted.")
            logs.append({
                "action": action,
                "details": str(details)[:300],
                "time": datetime.now(timezone.utc).isoformat(),
            })
            del logs[:-100]
            return result

    @staticmethod
    def _uid(uid):
        text = str(uid)
        if not text.isascii() or not text.isdecimal() or int(text) <= 0:
            raise AdminError("Invalid user ID.")
        return text

    def adjust_money(self, uid, amount, operation):
        uid = self._uid(uid)
        amount = nonnegative_amount(amount)

        def mutate(data):
            user = self.store.user(data, uid)
            old = user.get("money", 0)
            if type(old) is not int or old < 0:
                raise AdminError("Saved balance is invalid; operation aborted.")
            if operation == "add":
                value = old + amount
            elif operation == "remove":
                value = max(0, old - amount)
            elif operation == "set":
                value = amount
            else:
                raise AdminError("Unknown balance operation.")
            if operation in ("add", "set") and value > MAX_ADMIN_AMOUNT:
                raise AdminError(f"Balance cannot exceed {MAX_ADMIN_AMOUNT:,}.")
            user["money"] = value
            return value

        return self._apply(f"{operation}_money", f"user={uid}, amount={amount}", mutate)

    def give_money(self, amount):
        amount = nonnegative_amount(amount)

        def mutate(data):
            users = data.setdefault("users", {})
            for uid, user in users.items():
                value = nonnegative_amount(user.get("money", 0)) + amount
                if value > MAX_ADMIN_AMOUNT:
                    raise AdminError(f"Grant would exceed {MAX_ADMIN_AMOUNT:,} for user {uid}.")
                user["money"] = value
            return len(users)

        return self._apply("give_money", f"amount={amount}, players=all", mutate)

    def add_lands(self, uid, count):
        uid = self._uid(uid)
        count = nonnegative_amount(count, cap=100)
        if not 1 <= count <= 100:
            raise AdminError("Land count must be from 1 to 100.")

        def mutate(data):
            user = self.store.user(data, uid)
            for _ in range(count):
                user["lands"].append({
                    "building": None, "level": 0, "last_collect": time.time()
                })
            return len(user["lands"])

        return self._apply("add_lands", f"user={uid}, count={count}", mutate)

    def set_bonus_income(self, uid, amount):
        uid = self._uid(uid)
        amount = nonnegative_amount(amount)

        def mutate(data):
            user = self.store.user(data, uid)
            # Pay any earnings at the previous rate before replacing it.
            now = time.time()
            GameManager(self.store)._collect_user(user, now)
            user["bonus_income"] = amount
            user["bonus_last_collect"] = now

        self._apply("set_bonus_income", f"user={uid}, hourly_amount={amount}", mutate)

    def block_user(self, uid):
        uid = self._uid(uid)

        def mutate(data):
            current = data.setdefault("blocked_users", [])
            blocked = {str(item) for item in (current.keys() if isinstance(current, dict) else current)}
            if uid in blocked:
                raise AdminError("That player is already blocked.")
            blocked.add(uid)
            data["blocked_users"] = sorted(blocked)

        self._apply("block_user", f"user={uid}", mutate)

    def unblock_user(self, uid):
        uid = self._uid(uid)

        def mutate(data):
            current = data.setdefault("blocked_users", [])
            blocked = {str(item) for item in (current.keys() if isinstance(current, dict) else current)}
            if uid not in blocked:
                raise AdminError("That player is not blocked.")
            blocked.discard(uid)
            data["blocked_users"] = sorted(blocked)

        self._apply("unblock_user", f"user={uid}", mutate)

    def reset_all(self):
        def mutate(data):
            count = len(data.get("users", {}))
            data["users"] = {}
            data["lottery"] = {"tickets": {}, "pot": 0}
            data["cooldowns"] = {}
            data.pop("bonus_income", None)
            data["blocked_users"] = []
            return count

        return self._apply("reset_all", "all player game state reset", mutate)

    def reset_user(self, uid):
        uid = self._uid(uid)

        def mutate(data):
            users = data.setdefault("users", {})
            if uid not in users:
                raise AdminError("That player has no saved game data.")
            del users[uid]
            lottery = data.setdefault("lottery", {"tickets": {}, "pot": 0})
            tickets = lottery.setdefault("tickets", {})
            tickets.pop(uid, None)
            # Recompute from remaining tickets so removed stake is not stranded
            # in the pot without an eligible ticket holder.
            lottery["pot"] = sum(nonnegative_amount(ticket) for ticket in tickets.values())
            cooldowns = data.setdefault("cooldowns", {})
            for key in list(cooldowns):
                if uid in re.split(r"[:_\-]", str(key)):
                    del cooldowns[key]
            current = data.setdefault("blocked_users", [])
            if isinstance(current, dict):
                current.pop(uid, None)
            else:
                data["blocked_users"] = [str(item) for item in current if str(item) != uid]

        self._apply("reset_user", f"user={uid}", mutate)

    def clear_logs(self):
        def mutate(data):
            data["admin_logs"] = []

        # _apply writes a fresh audit entry after clearing the old entries.
        self._apply("clear_logs", "previous audit history cleared", mutate)

    def reset_cooldowns(self):
        def mutate(data):
            cooldowns = data.setdefault("cooldowns", {})
            count = len(cooldowns)
            data["cooldowns"] = {}
            return count

        return self._apply("reset_cooldowns", "all game cooldowns reset", mutate)

    def record_backup(self):
        with self.store.transaction() as data:
            backup = self.create_backup(state=data)
            self._append_log(data, "backup", "full store snapshot created")
            return backup

    def reset_level_profile(self, guild_id, user_id):
        """Back up and remove one guild-scoped XP profile as one transaction."""
        guild_key = self._uid(guild_id)
        user_key = self._uid(user_id)

        def mutate(data):
            levels = data.get("levels", {})
            if not isinstance(levels, dict):
                raise AdminError("Level data is invalid; operation aborted.")
            profiles = levels.get(guild_key, {})
            if not isinstance(profiles, dict):
                raise AdminError("Guild level data is invalid; operation aborted.")
            return profiles.pop(user_key, None) is not None

        return self._apply(
            "reset_level_profile",
            f"guild={guild_key}, user={user_key}",
            mutate,
        )

    @staticmethod
    def _append_log(data, action, details):
        logs = data.setdefault("admin_logs", [])
        logs.append({
            "action": action,
            "details": str(details)[:300],
            "time": datetime.now(timezone.utc).isoformat(),
        })
        del logs[:-100]

    def record_broadcast(self, sent, failed):
        # The external sends cannot be rolled back; still audit their aggregate.
        with self.store.transaction() as data:
            self._append_log(data, "broadcast", f"sent={sent}, failed={failed}")

    def snapshot(self):
        return copy.deepcopy(self.store.snapshot())