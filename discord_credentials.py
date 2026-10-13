"""Select the Discord credential for the explicitly configured bot environment."""

import os
from collections.abc import Mapping


def select_discord_token(environ: Mapping[str, str] | None = None) -> str:
    """Return the development or production token without cross-environment fallback."""
    environment = os.environ if environ is None else environ
    bot_environment = environment.get("DISCORD_BOT_ENV", "production").strip().lower()

    if bot_environment == "development":
        token = environment.get("DISCORD_TEST_TOKEN")
        if not token:
            raise RuntimeError(
                "Missing DISCORD_TEST_TOKEN for the development bot. Add a separate "
                "development-bot token to Replit Secrets; DISCORD_TOKEN is never "
                "used for development."
            )
        return token

    if bot_environment == "production":
        token = environment.get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError(
                "Missing DISCORD_TOKEN for the production bot. Add it to the server "
                "environment or the .env file next to bot.py."
            )
        return token

    raise RuntimeError(
        "Unsupported DISCORD_BOT_ENV value "
        f"{bot_environment!r}; use 'development' or 'production'."
    )