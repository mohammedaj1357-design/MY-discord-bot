"""Select persistent data sources without letting development touch production."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class BotStorageConfig:
    environment: str
    data_dir: Path
    game_store_backend: str
    database_url: str | None
    gateway_lock_enabled: bool

    @property
    def pending_invite_reports_path(self) -> Path:
        """Keep failed invite report recovery data inside the selected store."""
        return self.data_dir / "pending_invite_reports.json"


def resolve_bot_storage(
    script_dir: Path | str,
    environ: Mapping[str, str],
) -> BotStorageConfig:
    """Resolve isolated development storage and unchanged production storage."""
    root = Path(script_dir).resolve()
    production_data_dir = (root / "data").resolve()
    default_development_data_dir = (production_data_dir / "development").resolve()
    environment = environ.get("DISCORD_BOT_ENV", "production").strip().lower()

    if environment == "development":
        if environ.get("DATABASE_URL", "").strip():
            raise RuntimeError(
                "Development mode refuses DATABASE_URL because it may point to "
                "live production data. Development uses isolated local JSON "
                "storage; unset DATABASE_URL before startup."
            )

        configured_dir = environ.get("DISCORD_BOT_DATA_DIR", "data/development").strip()
        if not configured_dir:
            raise RuntimeError(
                "DISCORD_BOT_DATA_DIR cannot be empty in development mode."
            )

        data_dir = Path(configured_dir).expanduser()
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        data_dir = data_dir.resolve()

        is_within_production = (
            data_dir == production_data_dir
            or production_data_dir in data_dir.parents
        )
        is_above_production = data_dir in production_data_dir.parents
        is_within_dev_storage = (
            data_dir == default_development_data_dir
            or default_development_data_dir in data_dir.parents
        )
        if (
            (is_within_production and not is_within_dev_storage)
            or is_above_production
        ):
            raise RuntimeError(
                "Development bot storage overlaps the production data directory "
                f"({production_data_dir}). Set DISCORD_BOT_DATA_DIR to a "
                "development-only directory such as data/development."
            )

        return BotStorageConfig(
            environment="development",
            data_dir=data_dir,
            game_store_backend="json",
            database_url=None,
            gateway_lock_enabled=False,
        )

    if environment == "production":
        return BotStorageConfig(
            environment="production",
            data_dir=production_data_dir,
            game_store_backend="postgres",
            database_url=environ.get("DATABASE_URL"),
            gateway_lock_enabled=True,
        )

    raise RuntimeError(
        "DISCORD_BOT_ENV must be either 'development' or 'production'; "
        f"received {environment!r}."
    )