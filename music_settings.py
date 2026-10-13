"""Small atomic, guild-keyed music settings persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class MusicSettingsStore:
    DEFAULTS = {"volume": 100, "loop_mode": "off"}
    MODES = {"off", "track", "queue"}

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._settings: dict[str, dict[str, object]] = {}
        self._load()

    @staticmethod
    def _validate(guild_id: int, volume: int, loop_mode: str) -> None:
        if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
            raise ValueError("Invalid guild ID.")
        if type(volume) is not int or not 0 <= volume <= 200:
            raise ValueError("Volume must be between 0 and 200.")
        if not isinstance(loop_mode, str) or loop_mode not in MusicSettingsStore.MODES:
            raise ValueError("Invalid loop mode.")

    def get(self, guild_id: int) -> dict[str, object]:
        value = self._settings.get(str(guild_id), self.DEFAULTS)
        return {"volume": int(value["volume"]), "loop_mode": str(value["loop_mode"])}

    def get_settings(self, guild_id: int) -> dict[str, object]:
        return self.get(guild_id)

    def set(self, guild_id: int, volume: int, loop_mode: str) -> dict[str, object]:
        self._validate(guild_id, volume, loop_mode)
        key = str(guild_id)
        previous = self._settings.get(key)
        self._settings[key] = {"volume": volume, "loop_mode": loop_mode}
        try:
            self._save()
        except OSError:
            if previous is None:
                self._settings.pop(key, None)
            else:
                self._settings[key] = previous
            raise
        return self.get(guild_id)

    def set_settings(self, guild_id: int, settings: dict[str, object]) -> dict[str, object]:
        if not isinstance(settings, dict) or set(settings) != {"volume", "loop_mode"}:
            raise ValueError("Invalid music settings.")
        return self.set(guild_id, settings["volume"], settings["loop_mode"])

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid music settings.")
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, dict):
                volume, mode = value.get("volume"), value.get("loop_mode")
                if (
                    key.isdigit()
                    and type(volume) is int
                    and 0 <= volume <= 200
                    and type(mode) is str
                    and mode in self.MODES
                ):
                    self._settings[key] = {"volume": volume, "loop_mode": mode}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            temp_path = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(self._settings, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)