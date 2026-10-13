"""Loopback-only settings API for the authenticated web dashboard."""

from __future__ import annotations

import base64
import asyncio
import concurrent.futures
import hashlib
import hmac
import ipaddress
import os
import re

import discord
from flask import Blueprint, jsonify, request

from health_status import BotHealthStatus


GUILD_ID_PATTERN = re.compile(r"^[0-9]{17,20}$")
MAX_TIMEOUT_MINUTES = 10_080
MAX_SETTING_IDS = 100


def _expected_authorization() -> str | None:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        return None
    digest = hmac.new(
        secret.encode("utf-8"),
        b"discord-dashboard-internal-v1",
        hashlib.sha256,
    ).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"Bearer {token}"


def _is_loopback(address: str | None) -> bool:
    if not address:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def create_dashboard_internal_blueprint(
    bot,
    anti_link_store,
    channel_settings,
    *,
    bot_health: BotHealthStatus,
    maintenance_store=None,
    profanity_term_store=None,
    guild_profanity_term_store=None,
    music_manager=None,
    interruption_history=None,
    run_on_bot_loop=None,
):
    """Create internal routes around bot-owned state, never direct file edits."""
    blueprint = Blueprint("dashboard_internal", __name__)

    def authorized():
        expected = _expected_authorization()
        supplied = request.headers.get("Authorization", "")
        return (
            _is_loopback(request.remote_addr)
            and expected is not None
            and hmac.compare_digest(supplied, expected)
        )

    def denied():
        return jsonify({"error": "Internal dashboard access denied."}), 403

    def maintenance_snapshot():
        snapshot = maintenance_store.snapshot()
        snapshot["history"] = [
            {
                "enabled": change["enabled"],
                "message": change["message"],
                "source": change["source"],
                "changedAt": change["changed_at"],
            }
            for change in snapshot["history"]
        ]
        return snapshot

    @blueprint.get("/__internal/dashboard/interruption-history")
    def get_interruption_history():
        if not authorized():
            return denied()
        if interruption_history is None:
            return jsonify({"error": "Interruption history is unavailable."}), 503
        response = jsonify({"events": interruption_history.snapshot()})
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/__internal/dashboard/maintenance")
    def get_maintenance():
        if not authorized():
            return denied()
        if maintenance_store is None:
            return jsonify({"error": "Maintenance settings are unavailable."}), 503
        response = jsonify(maintenance_snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.patch("/__internal/dashboard/maintenance")
    def update_maintenance():
        if not authorized():
            return denied()
        if maintenance_store is None:
            return jsonify({"error": "Maintenance settings are unavailable."}), 503

        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"enabled", "message"}
            or type(payload.get("enabled")) is not bool
            or not isinstance(payload.get("message"), str)
        ):
            return jsonify({"error": "Invalid maintenance settings."}), 400

        try:
            maintenance_store.set_state(
                payload["enabled"],
                payload["message"],
                source="dashboard",
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except OSError:
            return jsonify({"error": "Maintenance settings could not be saved."}), 500

        response = jsonify(maintenance_snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    def get_ready_guild(guild_id):
        if not authorized():
            return None, denied()
        if not bot.is_ready():
            return None, (jsonify({"error": "The bot is not ready."}), 503)
        raw_id = str(guild_id)
        if not GUILD_ID_PATTERN.fullmatch(raw_id):
            return None, (jsonify({"error": "Invalid server ID."}), 400)
        guild = bot.get_guild(int(raw_id))
        if guild is None:
            return None, (jsonify({"error": "The bot is not in this server."}), 404)
        return guild, None

    def guild_summary(guild):
        icon = getattr(guild, "icon", None)
        return {
            "id": str(guild.id),
            "name": guild.name,
            "iconUrl": icon.url if icon else None,
            "memberCount": guild.member_count,
        }

    def setting_id_list(value, lookup, expected_type, field):
        if (
            not isinstance(value, list)
            or len(value) > MAX_SETTING_IDS
            or any(
                not isinstance(item, str)
                or not GUILD_ID_PATTERN.fullmatch(item)
                for item in value
            )
            or len(value) != len(set(value))
        ):
            raise ValueError(f"Invalid {field}.")
        resolved = []
        for raw_id in value:
            item = lookup(int(raw_id))
            if item is None or not isinstance(item, expected_type):
                raise ValueError(f"One or more {field} do not belong to this server.")
            if isinstance(item, discord.Role) and item.is_default():
                raise ValueError("The everyone role cannot be exempted.")
            resolved.append(int(raw_id))
        return resolved

    def on_bot_loop(coroutine):
        """Run a manager coroutine on Discord's loop, never Flask's thread loop."""
        if run_on_bot_loop is not None:
            return run_on_bot_loop(coroutine)
        loop = getattr(bot, "loop", None)
        if loop is None or not loop.is_running():
            coroutine.close()
            raise RuntimeError("The bot event loop is unavailable.")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=5)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError("The bot event loop did not respond in time.") from exc

    def filter_dto(guild):
        base = profanity_term_store.list_terms()
        overrides = guild_profanity_term_store.list_overrides(guild.id, base)
        return {
            "effectiveTerms": guild_profanity_term_store.list_terms(guild.id, base),
            "addedTerms": overrides["added"],
            "removedTerms": overrides["removed"],
        }

    async def filter_snapshot(guild):
        return filter_dto(guild)

    @blueprint.get("/__internal/dashboard/guilds/<guild_id>/filter")
    def get_filter(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error
        if profanity_term_store is None or guild_profanity_term_store is None:
            return jsonify({"error": "Filter settings are unavailable."}), 503
        try:
            result = on_bot_loop(filter_snapshot(guild))
        except TimeoutError:
            return jsonify({"error": "Filter settings timed out."}), 504
        except RuntimeError:
            return jsonify({"error": "Filter settings are unavailable."}), 503
        return jsonify(result)

    @blueprint.patch("/__internal/dashboard/guilds/<guild_id>/filter")
    def update_filter(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error
        if profanity_term_store is None or guild_profanity_term_store is None:
            return jsonify({"error": "Filter settings are unavailable."}), 503
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"action", "term", "actorId"}
            or not isinstance(payload.get("action"), str)
            or payload.get("action") not in {"add", "remove"}
            or not isinstance(payload.get("term"), str)
            or not isinstance(payload.get("actorId"), str)
            or not re.fullmatch(r"[0-9]{17,20}", payload["actorId"])
        ):
            return jsonify({"error": "Invalid filter settings."}), 400
        async def change_filter():
            base = profanity_term_store.list_terms()
            method = (
                guild_profanity_term_store.add
                if payload["action"] == "add"
                else guild_profanity_term_store.remove
            )
            method(guild.id, payload["term"], int(payload["actorId"]), base)
            return filter_dto(guild)

        try:
            result = on_bot_loop(change_filter())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify({"error": "Filter settings could not be saved."}), 500
        except TimeoutError:
            return jsonify({"error": "Filter settings timed out."}), 504
        except RuntimeError:
            return jsonify({"error": "Filter settings are unavailable."}), 503
        return jsonify(result)

    @blueprint.get("/__internal/dashboard/guilds/<guild_id>/music")
    def get_music(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error
        if music_manager is None:
            return jsonify({"error": "Music settings are unavailable."}), 503
        try:
            settings = on_bot_loop(music_manager.settings_snapshot(guild.id))
        except TimeoutError:
            return jsonify({"error": "Music settings timed out."}), 504
        except RuntimeError:
            return jsonify({"error": "Music settings are unavailable."}), 503
        return jsonify({"volume": settings["volume"], "loopMode": settings["loop_mode"]})

    @blueprint.patch("/__internal/dashboard/guilds/<guild_id>/music")
    def update_music(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error
        if music_manager is None:
            return jsonify({"error": "Music settings are unavailable."}), 503
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"volume", "loopMode"}
            or type(payload.get("volume")) is not int
            or not 0 <= payload["volume"] <= 200
            or not isinstance(payload.get("loopMode"), str)
            or payload.get("loopMode") not in {"off", "track", "queue"}
        ):
            return jsonify({"error": "Invalid music settings."}), 400
        try:
            # The manager persists first, then updates runtime and PCM source.
            settings = on_bot_loop(
                music_manager.apply_settings(
                    guild.id, payload["volume"], payload["loopMode"]
                )
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify({"error": "Music settings could not be saved."}), 500
        except TimeoutError:
            return jsonify({"error": "Music settings timed out."}), 504
        except RuntimeError:
            return jsonify({"error": "Music settings are unavailable."}), 503
        return jsonify({"volume": settings["volume"], "loopMode": settings["loop_mode"]})

    @blueprint.get("/__internal/dashboard/guilds")
    def list_guilds():
        if not authorized():
            return denied()
        if not bot.is_ready():
            return jsonify({"error": "The bot is not ready."}), 503
        guilds = sorted(bot.guilds, key=lambda guild: guild.name.casefold())
        return jsonify({"guilds": [guild_summary(guild) for guild in guilds]})

    @blueprint.get("/__internal/dashboard/guilds/<guild_id>")
    def get_guild_overview(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error

        bot_member = guild.me
        text_channels = []
        for channel in sorted(guild.text_channels, key=lambda item: item.name.casefold()):
            permissions = (
                channel.permissions_for(bot_member) if bot_member is not None else None
            )
            text_channels.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "canSendMessages": bool(
                        permissions
                        and permissions.view_channel
                        and permissions.send_messages
                    ),
                }
            )

        roles = [
            {"id": str(role.id), "name": role.name}
            for role in sorted(guild.roles, key=lambda item: item.name.casefold())
            if not role.is_default()
        ]
        settings = anti_link_store.get(guild.id)
        return jsonify(
            {
                "guild": guild_summary(guild),
                "botEnvironment": bot_health.environment,
                "antiLink": {
                    "enabled": settings["enabled"],
                    "timeoutMinutes": settings["timeout_minutes"],
                    "allowedChannelIds": [
                        str(value) for value in settings["allowed_channels"]
                    ],
                    "exemptRoleIds": [
                        str(value) for value in settings["exempt_roles"]
                    ],
                },
                "responseChannel": {
                    "channelId": (
                        str(channel_settings.get(guild.id))
                        if channel_settings.get(guild.id) is not None
                        else None
                    )
                },
                "textChannels": text_channels,
                "roles": roles,
            }
        )

    @blueprint.patch("/__internal/dashboard/guilds/<guild_id>/anti-link")
    def update_anti_link(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error

        payload = request.get_json(silent=True)
        expected_keys = {
            "enabled",
            "timeoutMinutes",
            "allowedChannelIds",
            "exemptRoleIds",
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            return jsonify({"error": "Invalid anti-link settings."}), 400

        minutes = payload["timeoutMinutes"]
        if (
            type(payload["enabled"]) is not bool
            or type(minutes) is not int
            or not 0 <= minutes <= MAX_TIMEOUT_MINUTES
        ):
            return jsonify({"error": "Invalid anti-link settings."}), 400

        try:
            allowed_channels = setting_id_list(
                payload["allowedChannelIds"],
                guild.get_channel,
                discord.TextChannel,
                "allowed channels",
            )
            exempt_roles = setting_id_list(
                payload["exemptRoleIds"],
                guild.get_role,
                discord.Role,
                "exempt roles",
            )
            saved = anti_link_store.set_settings(
                guild.id,
                {
                    "enabled": payload["enabled"],
                    "timeout_minutes": minutes,
                    "allowed_channels": allowed_channels,
                    "exempt_roles": exempt_roles,
                },
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except OSError:
            return jsonify({"error": "Anti-link settings could not be saved."}), 500

        return jsonify(
            {
                "enabled": saved["enabled"],
                "timeoutMinutes": saved["timeout_minutes"],
                "allowedChannelIds": [
                    str(value) for value in saved["allowed_channels"]
                ],
                "exemptRoleIds": [
                    str(value) for value in saved["exempt_roles"]
                ],
            }
        )

    @blueprint.patch("/__internal/dashboard/guilds/<guild_id>/response-channel")
    def update_response_channel(guild_id):
        guild, error = get_ready_guild(guild_id)
        if error:
            return error

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"channelId"}:
            return jsonify({"error": "Invalid response channel."}), 400
        raw_channel_id = payload["channelId"]
        if raw_channel_id is None:
            channel_settings.set(guild.id, None)
            return jsonify({"channelId": None})
        if (
            not isinstance(raw_channel_id, str)
            or not GUILD_ID_PATTERN.fullmatch(raw_channel_id)
        ):
            return jsonify({"error": "Invalid response channel."}), 400

        channel = guild.get_channel(int(raw_channel_id))
        if not isinstance(channel, discord.TextChannel):
            return jsonify({"error": "The channel is not a text channel in this server."}), 400
        bot_member = guild.me
        permissions = (
            channel.permissions_for(bot_member) if bot_member is not None else None
        )
        if not (
            permissions
            and permissions.view_channel
            and permissions.send_messages
        ):
            return jsonify({"error": "The bot cannot send messages in that channel."}), 400

        channel_settings.set(guild.id, channel.id)
        return jsonify({"channelId": str(channel.id)})

    return blueprint