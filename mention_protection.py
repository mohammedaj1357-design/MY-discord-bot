"""Persistent protection against mentions of configured Discord members."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import discord


logger = logging.getLogger(__name__)
WARNING_SECONDS = 5
MAX_ATTEMPTS = 3
WARNING_EXPIRATION = timedelta(hours=24)
TIMEOUT_DURATION = timedelta(minutes=10)
MASS_MENTION_TIMEOUT_DURATION = timedelta(minutes=30)
MASS_MENTION_DELETE_ALERT_COOLDOWN = timedelta(minutes=15)


class MentionProtection:
    def __init__(
        self,
        developer_id: int,
        *,
        protected_users_path: Path | str | None = None,
        warnings_path: Path | str | None = None,
        mass_mention_targets_path: Path | str | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        warning_expiration: timedelta = WARNING_EXPIRATION,
        timeout_duration: timedelta = TIMEOUT_DURATION,
        clock: Callable[[], datetime] | None = None,
    ):
        self.developer_id = int(developer_id)
        data_dir = Path(__file__).parent / "data"
        self.protected_users_path = Path(
            protected_users_path or data_dir / "protected_users.json"
        )
        self.warnings_path = Path(
            warnings_path or data_dir / "mention_warnings.json"
        )
        self.mass_mention_targets_path = Path(
            mass_mention_targets_path or data_dir / "mass_mention_targets.json"
        )
        self.max_attempts = max_attempts
        self.warning_expiration = warning_expiration
        self.timeout_duration = timeout_duration
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.protected_users: set[int] = {self.developer_id}
        self._warnings: dict[tuple[int, int], list[tuple[datetime, str]]] = {}
        self.mass_mention_members: set[int] = set()
        self.mass_mention_roles: set[int] = set()
        self._mass_mention_delete_alerts: dict[int, tuple[datetime, int]] = {}
        self._load_protected_users()
        self._load_warnings()
        self._load_mass_mention_targets()

    def add_mass_mention_member(self, user_id: int) -> bool:
        return self._change_mass_mention_target("members", user_id, add=True)

    def remove_mass_mention_member(self, user_id: int) -> bool:
        return self._change_mass_mention_target("members", user_id, add=False)

    def add_mass_mention_role(self, role_id: int) -> bool:
        return self._change_mass_mention_target("roles", role_id, add=True)

    def remove_mass_mention_role(self, role_id: int) -> bool:
        return self._change_mass_mention_target("roles", role_id, add=False)

    def list_mass_mention_targets(self) -> dict[str, list[int]]:
        return {
            "members": sorted(self.mass_mention_members),
            "roles": sorted(self.mass_mention_roles),
        }

    def _change_mass_mention_target(
        self,
        target_type: str,
        target_id: int,
        *,
        add: bool,
    ) -> bool:
        target_id = int(target_id)
        targets = (
            self.mass_mention_members
            if target_type == "members"
            else self.mass_mention_roles
        )
        if target_id <= 0 or ((target_id in targets) == add):
            return False
        if add:
            targets.add(target_id)
        else:
            targets.remove(target_id)
        try:
            self._save_mass_mention_targets()
        except OSError:
            if add:
                targets.discard(target_id)
            else:
                targets.add(target_id)
            raise
        return True

    def add_protected(self, user_id: int) -> bool:
        user_id = int(user_id)
        if user_id in self.protected_users:
            return False
        self.protected_users.add(user_id)
        try:
            self._save_protected_users()
        except OSError:
            self.protected_users.discard(user_id)
            raise
        return True

    def remove_protected(self, user_id: int) -> bool:
        user_id = int(user_id)
        if user_id == self.developer_id or user_id not in self.protected_users:
            return False
        self.protected_users.remove(user_id)
        try:
            self._save_protected_users()
        except OSError:
            self.protected_users.add(user_id)
            raise
        return True

    def list_protected(self) -> list[int]:
        return sorted(self.protected_users)

    def add_warning(self, user_id: int, guild_id: int, reason: str) -> int:
        key = (int(guild_id), int(user_id))
        warnings = self._active_warnings(key)
        warnings.append((self._now(), reason))
        self._warnings[key] = warnings
        try:
            self._save_warnings()
        except OSError:
            logger.exception("Could not persist mention warnings.")
        return len(warnings)

    def get_warnings(self, user_id: int, guild_id: int) -> int:
        key = (int(guild_id), int(user_id))
        warnings = self._warnings.get(key, [])
        active = self._active_warnings(key)
        if len(active) != len(warnings):
            try:
                self._save_warnings()
            except OSError:
                logger.exception("Could not persist expired mention warnings.")
        return len(active)

    def clear_warnings(self, user_id: int, guild_id: int) -> int:
        key = (int(guild_id), int(user_id))
        count = self.get_warnings(user_id, guild_id)
        if not count:
            return 0
        previous = self._warnings.pop(key, [])
        try:
            self._save_warnings()
        except OSError:
            self._warnings[key] = previous
            raise
        return count

    async def handle(self, message: discord.Message, client: discord.Client) -> bool:
        """Enforce the consecutive protected-mention limit for a member."""
        if message.guild is None or message.author.bot:
            return False
        if message.author.id == self.developer_id:
            self._reset_warning_streak(message.author.id, message.guild.id)
            return False

        permissions = getattr(message.author, "guild_permissions", None)
        if permissions and permissions.administrator:
            self._reset_warning_streak(message.author.id, message.guild.id)
            return False
        if message.author.id in self.protected_users:
            self._reset_warning_streak(message.author.id, message.guild.id)
            return False

        mentioned_ids = sorted(
            {
                user.id
                for user in message.mentions
                if user.id in self.protected_users
            }
        )
        if not mentioned_ids:
            self._reset_warning_streak(message.author.id, message.guild.id)
            return False

        try:
            await message.delete()
        except discord.NotFound:
            return True
        except discord.Forbidden:
            logger.warning(
                "Could not delete a protected-member mention in guild %s, channel %s; "
                "the bot needs Manage Messages.",
                message.guild.id,
                message.channel.id,
            )
            return True
        except discord.HTTPException:
            logger.exception(
                "Discord rejected deletion of a protected-member mention in guild %s, channel %s.",
                message.guild.id,
                message.channel.id,
            )
            return True

        try:
            warning_count = self.add_warning(
                message.author.id,
                message.guild.id,
                "منشن عضو محمي",
            )
        except Exception:
            logger.exception("Could not record a protected-member mention.")
            warning_count = self.get_warnings(
                message.author.id,
                message.guild.id,
            )

        muted = False
        timeout_failed = False
        timeout_error_details: str | None = None
        if warning_count >= self.max_attempts:
            try:
                await message.author.timeout(
                    self.timeout_duration,
                    reason="Repeatedly mentioning a protected member",
                )
            except discord.Forbidden as error:
                timeout_failed = True
                timeout_error_details = self._timeout_failure_details(
                    message,
                    client,
                    error,
                )
                logger.warning(
                    "Could not time out member %s in guild %s: %s",
                    message.author.id,
                    message.guild.id,
                    timeout_error_details,
                )
            except discord.HTTPException as error:
                timeout_failed = True
                timeout_error_details = self._timeout_failure_details(
                    message,
                    client,
                    error,
                )
                logger.warning(
                    "Discord rejected a timeout for member %s in guild %s: %s",
                    message.author.id,
                    message.guild.id,
                    timeout_error_details,
                )
            except Exception as error:
                timeout_failed = True
                timeout_error_details = self._timeout_failure_details(
                    message,
                    client,
                    error,
                )
                logger.exception(
                    "Unexpected error while timing out member %s in guild %s: %s",
                    message.author.id,
                    message.guild.id,
                    timeout_error_details,
                )
            else:
                muted = True
                logger.info(
                    "Applied a %.1f-minute timeout to member %s in guild %s "
                    "after warning %s/%s.",
                    self.timeout_duration.total_seconds() / 60,
                    message.author.id,
                    message.guild.id,
                    warning_count,
                    self.max_attempts,
                )
                try:
                    self.clear_warnings(message.author.id, message.guild.id)
                except OSError:
                    logger.exception(
                        "Could not persist warning reset after a successful timeout."
                    )
                    self._warnings.pop((message.guild.id, message.author.id), None)

        await self._send_warning(
            message,
            warning_count,
            muted,
            timeout_failed,
        )
        await self._notify_developer(
            client,
            message,
            warning_count,
            mentioned_ids,
            muted,
            timeout_failed,
            timeout_error_details,
        )
        return True

    async def handle_mass_mention(
        self,
        message: discord.Message,
        client: discord.Client,
    ) -> bool:
        """Delete configured members' mass mentions and apply a fixed timeout."""
        if (
            message.guild is None
            or message.author.bot
            or not getattr(message, "mention_everyone", False)
        ):
            return False

        author_role_ids = {
            role.id for role in getattr(message.author, "roles", ())
        }
        if (
            message.author.id not in self.mass_mention_members
            and not author_role_ids.intersection(self.mass_mention_roles)
        ):
            return False

        try:
            await message.delete()
        except discord.NotFound:
            # The message is already gone, so continue with the timeout.
            pass
        except discord.Forbidden as error:
            logger.warning(
                "Could not delete a configured mass mention by member %s in "
                "guild %s, channel %s; the bot needs Manage Messages: %s",
                message.author.id,
                message.guild.id,
                message.channel.id,
                error,
            )
            await self._report_mass_mention_delete_failure(
                message,
                client,
                error,
            )
            return True
        except discord.HTTPException as error:
            logger.exception(
                "Discord rejected deletion of a configured mass mention by "
                "member %s in guild %s, channel %s.",
                message.author.id,
                message.guild.id,
                message.channel.id,
            )
            await self._report_mass_mention_delete_failure(
                message,
                client,
                error,
            )
            return True

        try:
            await message.author.timeout(
                MASS_MENTION_TIMEOUT_DURATION,
                reason="Configured mass mention (@everyone/@here)",
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            details = self._timeout_failure_details(message, client, error)
            logger.warning(
                "Deleted a configured mass mention by member %s in guild %s, "
                "but the 30-minute timeout failed: %s",
                message.author.id,
                message.guild.id,
                details,
            )
            await self._report_mass_mention_timeout_failure(message, details)
        except Exception as error:
            details = self._timeout_failure_details(message, client, error)
            logger.exception(
                "Deleted a configured mass mention by member %s in guild %s, "
                "but the 30-minute timeout failed: %s",
                message.author.id,
                message.guild.id,
                details,
            )
            await self._report_mass_mention_timeout_failure(message, details)
        else:
            logger.info(
                "Applied a 30-minute timeout to member %s in guild %s after "
                "a configured mass mention.",
                message.author.id,
                message.guild.id,
            )
        return True

    async def _report_mass_mention_delete_failure(
        self,
        message: discord.Message,
        client: discord.Client,
        error: discord.HTTPException,
    ) -> None:
        guild_id = int(message.guild.id)
        suppressed_count = self._claim_mass_mention_delete_alert(guild_id)
        if suppressed_count is None:
            return

        status = getattr(error, "status", "unknown")
        code = getattr(error, "code", "unknown")
        failure_details = f"{type(error).__name__}; HTTP status={status}; Discord code={code}"
        if isinstance(error, discord.Forbidden):
            failure_details += "; the bot may be missing Manage Messages permission"

        guild_name = getattr(message.guild, "name", "Unknown server")
        channel_name = getattr(message.channel, "name", "unknown")
        member_name = getattr(message.author, "display_name", "Unknown member")
        text = (
            "⚠️ The bot could not delete a configured mass-mention message.\n"
            f"Member: {member_name} (ID: {message.author.id})\n"
            f"Server: {guild_name} (ID: {guild_id})\n"
            f"Channel: #{channel_name} (ID: {message.channel.id})\n"
            f"Failure: {failure_details}"
        )
        if suppressed_count:
            text += (
                "\nAdditional deletion failures suppressed since the previous "
                f"alert attempt: {suppressed_count}."
            )

        try:
            developer = client.get_user(self.developer_id)
            if developer is None:
                developer = await client.fetch_user(self.developer_id)
            await developer.send(
                text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.warning(
                "Could not deliver the private mass-mention deletion alert to "
                "developer %s for guild %s; Discord denied the DM.",
                self.developer_id,
                guild_id,
            )
        except discord.HTTPException as delivery_error:
            logger.warning(
                "Could not deliver the private mass-mention deletion alert to "
                "developer %s for guild %s (HTTP status=%s, code=%s).",
                self.developer_id,
                guild_id,
                getattr(delivery_error, "status", "unknown"),
                getattr(delivery_error, "code", "unknown"),
            )
        except Exception as delivery_error:
            logger.exception(
                "Unexpected failure delivering the private mass-mention "
                "deletion alert to developer %s for guild %s (%s).",
                self.developer_id,
                guild_id,
                type(delivery_error).__name__,
            )

    def _claim_mass_mention_delete_alert(self, guild_id: int) -> int | None:
        now = self._clock()
        previous = self._mass_mention_delete_alerts.get(guild_id)
        if (
            previous is not None
            and now - previous[0] < MASS_MENTION_DELETE_ALERT_COOLDOWN
        ):
            self._mass_mention_delete_alerts[guild_id] = (
                previous[0],
                previous[1] + 1,
            )
            return None

        suppressed_count = previous[1] if previous is not None else 0
        self._mass_mention_delete_alerts[guild_id] = (now, 0)
        return suppressed_count

    async def _report_mass_mention_timeout_failure(
        self,
        message: discord.Message,
        details: str,
    ) -> None:
        try:
            await message.channel.send(
                "حُذفت رسالة المنشن الجماعي، لكن تعذر تطبيق مهلة 30 دقيقة. "
                f"تحقق من صلاحية Moderate Members وترتيب رتبة البوت. التفاصيل: {details}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.warning(
                "Could not report the failed mass-mention timeout in guild %s, "
                "channel %s.",
                message.guild.id,
                message.channel.id,
            )

    def _reset_warning_streak(self, user_id: int, guild_id: int) -> None:
        """Reset this member's streak after a non-violating message."""
        key = (int(guild_id), int(user_id))
        if not self._warnings.get(key):
            return
        try:
            self.clear_warnings(user_id, guild_id)
        except OSError:
            logger.exception(
                "Could not persist consecutive-warning reset for member %s in guild %s.",
                user_id,
                guild_id,
            )
            self._warnings.pop(key, None)

    def _timeout_failure_details(
        self,
        message: discord.Message,
        client: discord.Client,
        error: Exception,
    ) -> str:
        guild = message.guild
        bot_user = getattr(client, "user", None)
        bot_member = getattr(guild, "me", None)
        if bot_member is None and bot_user is not None:
            get_member = getattr(guild, "get_member", None)
            if callable(get_member):
                bot_member = get_member(bot_user.id)

        bot_permissions = getattr(bot_member, "guild_permissions", None)
        target_permissions = getattr(message.author, "guild_permissions", None)
        bot_role = getattr(bot_member, "top_role", None)
        target_role = getattr(message.author, "top_role", None)
        status = getattr(error, "status", "unknown")
        code = getattr(error, "code", "unknown")
        response_text = getattr(error, "text", str(error))

        details = [
            f"HTTP status={status}, code={code}, response={response_text!r}",
            "bot Moderate Members="
            f"{getattr(bot_permissions, 'moderate_members', 'unknown')}",
            "bot Administrator="
            f"{getattr(bot_permissions, 'administrator', 'unknown')}",
            "target Administrator="
            f"{getattr(target_permissions, 'administrator', 'unknown')}",
        ]
        if bot_role is not None:
            details.append(
                "bot top role="
                f"{getattr(bot_role, 'name', 'unknown')} "
                f"(position={getattr(bot_role, 'position', 'unknown')})"
            )
        else:
            details.append("bot top role=unknown")
        if target_role is not None:
            details.append(
                "target top role="
                f"{getattr(target_role, 'name', 'unknown')} "
                f"(position={getattr(target_role, 'position', 'unknown')})"
            )
        else:
            details.append("target top role=unknown")

        bot_position = getattr(bot_role, "position", None)
        target_position = getattr(target_role, "position", None)
        likely_causes = []
        if getattr(bot_permissions, "moderate_members", None) is False:
            likely_causes.append("bot lacks Moderate Members")
        if getattr(target_permissions, "administrator", False):
            likely_causes.append("target has Administrator")
        if (
            bot_position is not None
            and target_position is not None
            and bot_position <= target_position
        ):
            likely_causes.append("bot top role is not above target")
        if likely_causes:
            details.append("likely causes=" + ", ".join(likely_causes))
        return "; ".join(details)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _active_warnings(
        self,
        key: tuple[int, int],
    ) -> list[tuple[datetime, str]]:
        cutoff = self._now() - self.warning_expiration
        previous = self._warnings.get(key, [])
        active = [(created_at, reason) for created_at, reason in previous if created_at > cutoff]
        if active:
            self._warnings[key] = active
        else:
            self._warnings.pop(key, None)
        return active

    def _load_protected_users(self) -> None:
        try:
            data = json.loads(self.protected_users_path.read_text(encoding="utf-8"))
            users = data.get("users") if isinstance(data, dict) else None
            if not isinstance(users, list):
                raise ValueError("protected user file must contain a users list")
            for user_id in users:
                if isinstance(user_id, (str, int)) and str(user_id).isdecimal():
                    parsed_id = int(user_id)
                    if parsed_id > 0:
                        self.protected_users.add(parsed_id)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            logger.exception("Could not load the protected user list.")

    def _load_warnings(self) -> None:
        try:
            data = json.loads(self.warnings_path.read_text(encoding="utf-8"))
            stored = data.get("warnings") if isinstance(data, dict) else None
            if not isinstance(stored, dict):
                raise ValueError("mention warning file must contain a warnings object")
            for raw_key, entries in stored.items():
                try:
                    guild_id, user_id = (int(value) for value in raw_key.split(":", 1))
                except (AttributeError, ValueError):
                    logger.warning("Ignoring malformed mention-warning key.")
                    continue
                if not isinstance(entries, list):
                    continue
                loaded: list[tuple[datetime, str]] = []
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("time"), str):
                        continue
                    try:
                        created_at = datetime.fromisoformat(entry["time"])
                    except ValueError:
                        logger.warning("Ignoring malformed mention-warning timestamp.")
                        continue
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    else:
                        created_at = created_at.astimezone(timezone.utc)
                    reason = entry.get("reason", "منشن عضو محمي")
                    loaded.append((created_at, str(reason)))
                if loaded:
                    self._warnings[(guild_id, user_id)] = loaded
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            logger.exception("Could not load persisted mention warnings.")

    def _load_mass_mention_targets(self) -> None:
        try:
            data = json.loads(
                self.mass_mention_targets_path.read_text(encoding="utf-8")
            )
            if not isinstance(data, dict):
                raise ValueError("mass mention target file must contain an object")
            for key, attribute in (
                ("members", "mass_mention_members"),
                ("roles", "mass_mention_roles"),
            ):
                values = data.get(key, [])
                if not isinstance(values, list):
                    raise ValueError(
                        f"mass mention target file must contain a {key} list"
                    )
                parsed = {
                    int(value)
                    for value in values
                    if isinstance(value, (str, int))
                    and str(value).isdecimal()
                    and int(value) > 0
                }
                setattr(self, attribute, parsed)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            logger.exception("Could not load configured mass-mention targets.")

    def _save_protected_users(self) -> None:
        self._write_json(
            self.protected_users_path,
            {"users": self.list_protected()},
        )

    def _save_warnings(self) -> None:
        stored = {
            f"{guild_id}:{user_id}": [
                {"time": created_at.isoformat(), "reason": reason}
                for created_at, reason in warnings
            ]
            for (guild_id, user_id), warnings in self._warnings.items()
            if warnings
        }
        self._write_json(self.warnings_path, {"warnings": stored})

    def _save_mass_mention_targets(self) -> None:
        self._write_json(
            self.mass_mention_targets_path,
            self.list_mass_mention_targets(),
        )

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)

    async def _send_warning(
        self,
        message: discord.Message,
        warning_count: int,
        muted: bool,
        timeout_failed: bool,
    ) -> None:
        if muted:
            text = "تم حذف رسالتك وإسكاتك 10 دقائق بسبب 3 منشنات متتالية لعضو محمي."
        elif timeout_failed:
            text = (
                f"وصلت إلى {warning_count}/{self.max_attempts} منشنات متتالية، "
                "لكن تعذر تطبيق المهلة. تحقق من صلاحية Moderate Members "
                "وترتيب رتبة البوت."
            )
        else:
            text = (
                "لا يمكنك منشن عضو محمي. هذه المنشن "
                f"{warning_count}/{self.max_attempts} متتالية خلال 24 ساعة؛ "
                "إرسال رسالة أخرى دون منشن محمي يعيد العدّ."
            )

        try:
            await message.channel.send(
                f"⚠️ {message.author.mention} {text}",
                delete_after=WARNING_SECONDS,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.warning(
                "Could not send a protected-mention warning in guild %s, channel %s.",
                message.guild.id,
                message.channel.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected a protected-mention warning in guild %s, channel %s.",
                message.guild.id,
                message.channel.id,
            )

    async def _notify_developer(
        self,
        client: discord.Client,
        message: discord.Message,
        warning_count: int,
        mentioned_ids: list[int],
        muted: bool,
        timeout_failed: bool,
        timeout_error_details: str | None,
    ) -> None:
        try:
            developer = client.get_user(self.developer_id)
            if developer is None:
                developer = await client.fetch_user(self.developer_id)
            if muted:
                outcome = "طُبّقت مهلة 10 دقائق."
            elif timeout_failed:
                outcome = (
                    "تعذر تطبيق المهلة؛ تحقق من الصلاحية وترتيب الرتب. "
                    f"تفاصيل الخطأ: {timeout_error_details or 'غير متوفرة'}"
                )
            else:
                outcome = "لم تُطبّق مهلة."
            text = (
                "🛡️ حُذفت رسالة بسبب منشن عضو محمي.\n"
                f"العضو: {message.author.display_name} (ID: {message.author.id})\n"
                f"السيرفر: {message.guild.name} (ID: {message.guild.id})\n"
                f"القناة: #{message.channel.name}\n"
                f"معرّفات المحميين: {', '.join(map(str, mentioned_ids))}\n"
                f"المنشنات المتتالية خلال 24 ساعة: {warning_count}/{self.max_attempts}\n"
                f"{outcome}"
            )
            await developer.send(
                text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.warning(
                "Could not DM developer %s about a protected mention in guild %s.",
                self.developer_id,
                message.guild.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Could not send protected-mention notification for guild %s.",
                message.guild.id,
            )