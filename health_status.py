"""Thread-safe readiness state shared by the bot and HTTP health endpoint."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
AI_PROVIDER_PROBE_INTERVAL_MINUTES = 15
AI_PROVIDER_PROBE_STALE_AFTER_INTERVALS = 2
AI_PROVIDER_PROBE_STALE_AFTER_SECONDS = (
    AI_PROVIDER_PROBE_INTERVAL_MINUTES
    * AI_PROVIDER_PROBE_STALE_AFTER_INTERVALS
    * 60
)


class BotInterruptionHistory:
    """Persist a bounded history of safe gateway and process lifecycle events."""

    MAX_EVENTS = 100
    VERSION = 1

    def __init__(
        self,
        path: Path | str,
        *,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = Path(path)
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._events: list[dict[str, object]] = []
        self._next_id = 1
        self._active_process_started_at: str | None = None
        self._load()

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _valid_timestamp(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    @staticmethod
    def _valid_duration(value: object) -> bool:
        return (
            value is None
            or (
                type(value) in (int, float)
                and math.isfinite(value)
                and value >= 0
            )
        )

    def _validated_event(self, value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        event_type = value.get("type")
        event_id = value.get("id")
        if type(event_id) is not int or event_id < 1:
            return None
        if event_type == "gateway_interruption":
            if (
                not self._valid_timestamp(value.get("started_at"))
                or (
                    value.get("recovered_at") is not None
                    and not self._valid_timestamp(value.get("recovered_at"))
                )
                or not self._valid_duration(value.get("duration_seconds"))
            ):
                return None
            return {
                "id": event_id,
                "type": event_type,
                "started_at": value["started_at"],
                "recovered_at": value.get("recovered_at"),
                "duration_seconds": value.get("duration_seconds"),
            }
        if event_type in {"process_started", "process_restarted"}:
            if not self._valid_timestamp(value.get("occurred_at")):
                return None
            return {
                "id": event_id,
                "type": event_type,
                "occurred_at": value["occurred_at"],
            }
        if event_type == "process_stopped":
            if (
                not self._valid_timestamp(value.get("occurred_at"))
                or not self._valid_duration(value.get("duration_seconds"))
            ):
                return None
            return {
                "id": event_id,
                "type": event_type,
                "occurred_at": value["occurred_at"],
                "duration_seconds": value.get("duration_seconds"),
            }
        return None

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
                raise ValueError("Unsupported interruption history format.")
            raw_events = payload.get("events")
            if not isinstance(raw_events, list):
                raise ValueError("Invalid interruption history events.")
            events = [
                event
                for raw_event in raw_events
                if (event := self._validated_event(raw_event)) is not None
            ][-self.MAX_EVENTS :]
            active_started_at = payload.get("active_process_started_at")
            if active_started_at is not None and not self._valid_timestamp(
                active_started_at
            ):
                raise ValueError("Invalid active process timestamp.")
            self._events = events
            self._next_id = max((int(event["id"]) for event in events), default=0) + 1
            self._active_process_started_at = active_started_at
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning(
                "Bot interruption history could not be loaded; starting a new history."
            )

    def _persist_locked(self) -> None:
        payload = {
            "version": self.VERSION,
            "events": self._events,
            "active_process_started_at": self._active_process_started_at,
        }
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, separators=(",", ":"), ensure_ascii=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError:
            logger.warning("Bot interruption history could not be saved.")

    def _append_locked(self, event: dict[str, object]) -> None:
        event["id"] = self._next_id
        self._next_id += 1
        self._events.append(event)
        self._events = self._events[-self.MAX_EVENTS :]

    def gateway_disconnected(self) -> None:
        """Start one outage record; duplicate gateway events do not duplicate it."""
        with self._lock:
            if any(
                event["type"] == "gateway_interruption"
                and event["recovered_at"] is None
                for event in self._events
            ):
                return
            self._append_locked(
                {
                    "type": "gateway_interruption",
                    "started_at": self._timestamp(self._wall_clock()),
                    "recovered_at": None,
                    "duration_seconds": None,
                }
            )
            self._persist_locked()

    def gateway_recovered(self, duration_seconds: float | None = None) -> None:
        """Close the current outage, including one that spans a process restart."""
        with self._lock:
            event = next(
                (
                    item
                    for item in reversed(self._events)
                    if item["type"] == "gateway_interruption"
                    and item["recovered_at"] is None
                ),
                None,
            )
            if event is None:
                return
            recovered_at = self._wall_clock()
            if duration_seconds is None:
                started_at = datetime.fromisoformat(
                    str(event["started_at"]).replace("Z", "+00:00")
                )
                duration_seconds = max(
                    0.0, (recovered_at - started_at).total_seconds()
                )
            event["recovered_at"] = self._timestamp(recovered_at)
            event["duration_seconds"] = round(max(0.0, duration_seconds), 1)
            self._persist_locked()

    def process_started(self) -> None:
        """Record a start, noting a restart only when a prior run was left active."""
        with self._lock:
            now = self._timestamp(self._wall_clock())
            event_type = (
                "process_restarted"
                if self._active_process_started_at is not None
                else "process_started"
            )
            self._append_locked({"type": event_type, "occurred_at": now})
            self._active_process_started_at = now
            self._persist_locked()

    def process_stopped(self, duration_seconds: float) -> None:
        """Record an observed process stop and clear its restart-detection marker."""
        with self._lock:
            if self._active_process_started_at is None:
                return
            self._append_locked(
                {
                    "type": "process_stopped",
                    "occurred_at": self._timestamp(self._wall_clock()),
                    "duration_seconds": round(max(0.0, duration_seconds), 1),
                }
            )
            self._active_process_started_at = None
            self._persist_locked()

    def snapshot(self) -> list[dict[str, object]]:
        """Return copies of bounded, safe event DTOs in most-recent-first order."""
        with self._lock:
            events = [dict(event) for event in self._events]
        events.sort(
            key=lambda event: (
                str(
                    event.get("recovered_at")
                    or event.get("occurred_at")
                    or event.get("started_at")
                ),
                int(event["id"]),
            ),
            reverse=True,
        )
        return events


class BotHealthStatus:
    """Track Discord connectivity and repeated failures of all AI providers."""

    def __init__(
        self,
        *,
        environment: str = "production",
        ai_failure_threshold: int = 3,
        ai_probe_failure_threshold: int = 2,
        clock: Callable[[], float] = time.monotonic,
        interruption_history: BotInterruptionHistory | None = None,
    ) -> None:
        if environment not in {"development", "production"}:
            raise ValueError("environment must be 'development' or 'production'")
        if ai_failure_threshold < 1:
            raise ValueError("ai_failure_threshold must be at least 1")
        if ai_probe_failure_threshold < 1:
            raise ValueError("ai_probe_failure_threshold must be at least 1")
        self.environment = environment
        self.ai_failure_threshold = ai_failure_threshold
        self.ai_probe_failure_threshold = ai_probe_failure_threshold
        self._clock = clock
        self._interruption_history = interruption_history
        self._lock = threading.Lock()
        self._ever_connected = False
        self._gateway_connected = False
        self._gateway_disconnect_count = 0
        self._gateway_disconnects_since_ready = 0
        self._gateway_disconnected_at: float | None = None
        self._ai_consecutive_failures = 0
        self._ai_has_succeeded = False
        self._ai_probe_consecutive_failures = 0
        self._ai_probe_has_succeeded = False
        self._ai_probe_monitoring_started_at: float | None = None
        self._ai_probe_completed_at: float | None = None

    def gateway_disconnected(self) -> dict[str, int | bool]:
        """Record a gateway disconnect and return safe log metadata."""
        with self._lock:
            was_connected = self._gateway_connected
            self._gateway_connected = False
            self._gateway_disconnect_count += 1
            self._gateway_disconnects_since_ready += 1
            if self._gateway_disconnected_at is None:
                self._gateway_disconnected_at = self._clock()
            if was_connected and self._interruption_history is not None:
                self._interruption_history.gateway_disconnected()
            return {
                "was_connected": was_connected,
                "disconnect_count": self._gateway_disconnect_count,
                "disconnects_since_ready": self._gateway_disconnects_since_ready,
            }

    def gateway_connected(self) -> dict[str, float | int | bool]:
        """Record a ready/resumed gateway session and report recovery details."""
        now = self._clock()
        with self._lock:
            was_disconnected = self._ever_connected and not self._gateway_connected
            outage_seconds = (
                max(0.0, now - self._gateway_disconnected_at)
                if self._gateway_disconnected_at is not None
                else 0.0
            )
            disconnects = self._gateway_disconnects_since_ready
            if self._ai_probe_monitoring_started_at is None:
                self._ai_probe_monitoring_started_at = now
            self._ever_connected = True
            self._gateway_connected = True
            self._gateway_disconnected_at = None
            self._gateway_disconnects_since_ready = 0
            if self._interruption_history is not None:
                self._interruption_history.gateway_recovered(
                    outage_seconds if was_disconnected else None
                )
            return {
                "was_disconnected": was_disconnected,
                "outage_seconds": outage_seconds,
                "disconnects": disconnects,
                "disconnect_count": self._gateway_disconnect_count,
            }

    def ai_request_failed(self) -> dict[str, int | bool]:
        """Record one conversation where no configured provider returned a reply."""
        with self._lock:
            self._ai_consecutive_failures += 1
            return {
                "consecutive_failures": self._ai_consecutive_failures,
                "degraded": self._ai_consecutive_failures
                >= self.ai_failure_threshold,
            }

    def ai_request_succeeded(self) -> int:
        """Record usable provider access and clear both failure streaks."""
        with self._lock:
            previous_failures = self._ai_consecutive_failures
            self._ai_consecutive_failures = 0
            self._ai_has_succeeded = True
            self._ai_probe_consecutive_failures = 0
            self._ai_probe_has_succeeded = True
            return previous_failures

    def ai_probe_failed(self) -> dict[str, int | bool]:
        """Record a failed check of every configured AI provider."""
        with self._lock:
            self._ai_probe_consecutive_failures += 1
            return {
                "consecutive_failures": self._ai_probe_consecutive_failures,
                "degraded": (
                    self._ai_probe_consecutive_failures
                    >= self.ai_probe_failure_threshold
                ),
            }

    def ai_probe_succeeded(self) -> int:
        """Clear the probe failure streak after any provider passes the check."""
        with self._lock:
            previous_failures = self._ai_probe_consecutive_failures
            self._ai_consecutive_failures = 0
            self._ai_has_succeeded = True
            self._ai_probe_consecutive_failures = 0
            self._ai_probe_has_succeeded = True
            return previous_failures

    def ai_probe_completed(self) -> None:
        """Record only the monotonic completion time, not provider response data."""
        now = self._clock()
        with self._lock:
            self._ai_probe_completed_at = now
            if self._ai_probe_monitoring_started_at is None:
                self._ai_probe_monitoring_started_at = now

    def snapshot(self) -> dict[str, object]:
        """Return a small public-safe status payload for uptime monitors."""
        with self._lock:
            if not self._ever_connected:
                gateway = "starting"
            elif self._gateway_connected:
                gateway = "connected"
            else:
                gateway = "disconnected"

            if (
                self._ai_probe_consecutive_failures
                >= self.ai_probe_failure_threshold
            ):
                ai_probe = "degraded"
            elif self._ai_probe_consecutive_failures:
                ai_probe = "intermittent"
            elif self._ai_probe_has_succeeded:
                ai_probe = "available"
            else:
                ai_probe = "unknown"

            has_completed_probe = self._ai_probe_completed_at is not None
            probe_freshness_reference = (
                self._ai_probe_completed_at
                if has_completed_probe
                else self._ai_probe_monitoring_started_at
            )
            if probe_freshness_reference is None:
                ai_probe_freshness = "unknown"
                ai_probe_stale = False
            else:
                seconds_since_reference = max(
                    0.0,
                    self._clock() - probe_freshness_reference,
                )
                ai_probe_stale = (
                    seconds_since_reference
                    >= AI_PROVIDER_PROBE_STALE_AFTER_SECONDS
                )
                if ai_probe_stale:
                    ai_probe_freshness = "stale"
                elif has_completed_probe:
                    ai_probe_freshness = "fresh"
                else:
                    ai_probe_freshness = "unknown"

            if (
                self._ai_consecutive_failures >= self.ai_failure_threshold
                or self._ai_probe_consecutive_failures
                >= self.ai_probe_failure_threshold
            ):
                ai_providers = "degraded"
            elif (
                self._ai_consecutive_failures
                or self._ai_probe_consecutive_failures
            ):
                ai_providers = "intermittent"
            elif self._ai_has_succeeded or self._ai_probe_has_succeeded:
                ai_providers = "available"
            else:
                ai_providers = "unknown"

            healthy = (
                gateway == "connected"
                and ai_providers != "degraded"
                and not ai_probe_stale
            )
            return {
                "environment": self.environment,
                "status": "healthy" if healthy else (
                    "starting" if gateway == "starting" else "unhealthy"
                ),
                "discord_gateway": gateway,
                "gateway_disconnect_count": self._gateway_disconnect_count,
                "ai_providers": ai_providers,
                "consecutive_ai_failures": self._ai_consecutive_failures,
                "ai_failure_threshold": self.ai_failure_threshold,
                "ai_provider_probe": ai_probe,
                "ai_provider_probe_freshness": ai_probe_freshness,
                "consecutive_ai_probe_failures": (
                    self._ai_probe_consecutive_failures
                ),
                "ai_probe_failure_threshold": self.ai_probe_failure_threshold,
            }


def create_health_blueprint(health_status: BotHealthStatus) -> Blueprint:
    """Create root and /healthz routes for a process readiness check."""
    blueprint = Blueprint("bot_health", __name__)

    @blueprint.get("/")
    @blueprint.get("/healthz")
    def health_check():
        status = health_status.snapshot()
        response = jsonify(status)
        response.headers["Cache-Control"] = "no-store"
        return response, 200 if status["status"] == "healthy" else 503

    return blueprint