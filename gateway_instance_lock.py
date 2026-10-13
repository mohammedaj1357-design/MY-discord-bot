"""Serialize Discord Gateway clients across overlapping VM deployments."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

# Stable PostgreSQL advisory-lock namespace/object keys for this bot. Production
# VM revisions share DATABASE_URL, so a new revision waits for the old one.
_LOCK_NAMESPACE = 0x5245504C
_LOCK_OBJECT = 0x44495343


class GatewayInstanceLock:
    """Hold a session-level PostgreSQL lock for the bot's process lifetime."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("DATABASE_URL is required for the Gateway instance lock")
        self._database_url = database_url
        self._connection = None

    def acquire(self) -> None:
        if self._connection is not None:
            raise RuntimeError("Gateway instance lock is already acquired")

        import psycopg

        connection = psycopg.connect(
            self._database_url,
            autocommit=True,
            connect_timeout=10,
            application_name="discord-gateway-instance-lock",
        )
        try:
            logger.info("Waiting for the Discord Gateway instance lock.")
            connection.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                (_LOCK_NAMESPACE, _LOCK_OBJECT),
            )
        except BaseException:
            try:
                connection.close()
            except Exception:
                logger.exception("Could not close the failed Gateway lock connection.")
            raise

        self._connection = connection
        logger.info("Acquired the Discord Gateway instance lock.")

    def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return

        try:
            connection.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (_LOCK_NAMESPACE, _LOCK_OBJECT),
            )
            logger.info("Released the Discord Gateway instance lock.")
        except Exception:
            # Closing the PostgreSQL session also releases its advisory lock.
            logger.exception("Could not explicitly release the Gateway instance lock.")
        finally:
            try:
                connection.close()
            except Exception:
                logger.exception("Could not close the Gateway lock connection.")