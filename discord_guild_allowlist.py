"""Fail-closed guild access policy for the development Discord bot."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
from typing import Any


MAX_DISCORD_GUILD_ID = (1 << 64) - 1


def _parse_guild_ids(raw_value: str | None) -> frozenset[int]:
    if raw_value is None or not raw_value.strip():
        raise RuntimeError(
            "Missing DISCORD_TEST_GUILD_IDS for the development bot. Set a "
            "comma-separated list of approved test-server IDs."
        )

    guild_ids: set[int] = set()
    for raw_id in raw_value.split(","):
        value = raw_id.strip()
        if not value or not value.isascii() or not value.isdecimal():
            raise RuntimeError(
                "Invalid DISCORD_TEST_GUILD_IDS. Use unique positive Discord "
                "server IDs separated by commas."
            )

        guild_id = int(value)
        if (
            guild_id <= 0
            or guild_id > MAX_DISCORD_GUILD_ID
            or value != str(guild_id)
            or guild_id in guild_ids
        ):
            raise RuntimeError(
                "Invalid DISCORD_TEST_GUILD_IDS. Use unique positive Discord "
                "server IDs separated by commas."
            )
        guild_ids.add(guild_id)

    return frozenset(guild_ids)


@dataclass(frozen=True)
class GuildAccessPolicy:
    """Allow all production guilds, but only explicitly approved test guilds."""

    environment: str
    allowed_guild_ids: frozenset[int]

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> GuildAccessPolicy:
        environment = os.environ if environ is None else environ
        bot_environment = (
            environment.get("DISCORD_BOT_ENV", "production").strip().lower()
        )

        if bot_environment == "production":
            return cls(environment=bot_environment, allowed_guild_ids=frozenset())
        if bot_environment != "development":
            raise RuntimeError(
                "Unsupported DISCORD_BOT_ENV value "
                f"{bot_environment!r}; use 'development' or 'production'."
            )

        return cls(
            environment=bot_environment,
            allowed_guild_ids=_parse_guild_ids(
                environment.get("DISCORD_TEST_GUILD_IDS")
            ),
        )

    def allows_guild(self, guild_id: int | None) -> bool:
        if self.environment == "production":
            return True
        return guild_id is not None and guild_id in self.allowed_guild_ids


async def leave_unapproved_guilds(
    guilds: Iterable[Any],
    policy: GuildAccessPolicy,
) -> tuple[list[Any], list[tuple[Any, Exception]]]:
    """Leave unapproved development guilds without affecting production guilds."""
    if policy.environment != "development":
        return [], []

    left = []
    failed = []
    for guild in tuple(guilds):
        if policy.allows_guild(guild.id):
            continue
        try:
            await guild.leave()
        except Exception as error:
            failed.append((guild, error))
        else:
            left.append(guild)
    return left, failed


def install_guild_access_checks(bot, policy: GuildAccessPolicy) -> None:
    """Apply the policy to prefix commands and application-command interactions."""

    async def prefix_command_check(ctx) -> bool:
        guild_id = ctx.guild.id if ctx.guild is not None else None
        return policy.allows_guild(guild_id)

    bot.add_check(prefix_command_check)

    original_interaction_check = bot.tree.interaction_check

    async def application_command_check(interaction) -> bool:
        if not policy.allows_guild(interaction.guild_id):
            return False
        return await original_interaction_check(interaction)

    bot.tree.interaction_check = application_command_check


if __name__ == "__main__":
    try:
        GuildAccessPolicy.from_environment()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    print("Development test-server allowlist is valid.")